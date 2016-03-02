# encoding: utf-8

# Nagstamon - Nagios status monitor for your desktop
# Copyright (C) 2008-2014 Henri Wahl <h.wahl@ifw-dresden.de> et al.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA

# Initial implementation by Marcus Mönnig
#
# This Server class connects against IcingaWeb2. The monitor URL in the setup should be
# something like http://icinga2/icingaweb2
#
# Status/TODOs:
#
# * The IcingaWeb2 API is not implemented yet, so currently this implementation uses
#   two HTTP requests per action. The first fetches the HTML, then the form data is extracted and
#   then a second HTTP POST request is made which actually executed the action.
#   Once IcingaWeb2 has an API, it's probably the better choice.


from Nagstamon.Servers.Generic import GenericServer
import urllib.request, urllib.parse, urllib.error
import sys
import copy
import json
import datetime
import webbrowser
from bs4 import BeautifulSoup
from Nagstamon.Objects import (GenericHost, GenericService, Result)
from Nagstamon.Helpers import not_empty
from Nagstamon.Config import (conf, AppInfo)
from collections import OrderedDict


def strfdelta(tdelta, fmt):
    d = {"days": tdelta.days}
    d["hours"], rem = divmod(tdelta.seconds, 3600)
    d["minutes"], d["seconds"] = divmod(rem, 60)
    return fmt.format(**d)


class Icinga2Server(GenericServer):
    """
        object of Incinga server
    """
    TYPE = u'Icinga2'
    MENU_ACTIONS = ['Monitor','Recheck','Acknowledge','Submit check result', 'Downtime']
    STATES_MAPPING = {'hosts' : {'0' : 'UP', '1' : 'DOWN', '2' : 'UNREACHABLE'},\
                     'services' : {'0' : 'OK', '1' : 'WARNING',  '2' : 'CRITICAL', '3' : 'UNKNOWN'}}
    STATES_MAPPING_REV = {'hosts' : { 'UP': '0', 'DOWN': '1', 'UNREACHABLE': '2'},\
                     'services' : {'OK': '0', 'WARNING': '1',  'CRITICAL': '2', 'UNKNOWN': '3'}}
    BROWSER_URLS = { 'monitor': '$MONITOR-CGI$/dashboard',\
                    'hosts': '$MONITOR-CGI$/monitoring/list/hosts',\
                    'services': '$MONITOR-CGI$/monitoring/list/services',\
                    'history': '$MONITOR-CGI$/monitoring/list/eventhistory?timestamp>=-7 days'}

    def init_config(self):
        """
            set URLs for CGI - they are static and there is no need to set them with every cycle
        """
        # dummy default empty cgi urls - get filled later when server version is known
        self.cgiurl_services = None
        self.cgiurl_hosts = None
        self.use_display_name_host = False
        self.use_display_name_service = False


    def init_HTTP(self):
        GenericServer.init_HTTP(self)

        if not 'Referer' in self.session.headers:
            self.session.headers['Referer'] = self.monitor_cgi_url + '/icingaweb2/monitoring'

        if len(self.session.cookies) == 0:
            # get login page, thus automatically a cookie
            login = self.FetchURL('{0}/authentication/login'.format(self.monitor_url))
            if login.error == '' and login.status_code == 200:
                form = login.result.find('form')
                form_inputs = {}
                for form_input in ('redirect', 'formUID', 'CSRFToken', 'btn_submit'):
                    form_inputs[form_input] = form.find('input', {'name': form_input})['value']
                form_inputs['username'] = self.username
                form_inputs['password'] = self.password

                # fire up login button with all needed data
                self.FetchURL('{0}/authentication/login'.format(self.monitor_url), cgi_data=form_inputs)


    def get_server_version(self):
        """
            Try to get Icinga version
        """
        result = self.FetchURL('%s/about' % (self.monitor_cgi_url), giveback='raw')

        if result.error != '':
            return result
        else:
            aboutraw = result.result

        aboutsoup = BeautifulSoup(aboutraw, 'html.parser')
        self.version =  aboutsoup.find('dt',text='Version').parent.findNext('dd').contents[0]


    def _get_status(self):
        """
            Get status from Icinga Server, prefer JSON if possible
        """
        try:
            if self.version == '':
                # we need to get the server version
                result = self.get_server_version()
            if self.version != '':
                # define CGI URLs for hosts and services
                if self.cgiurl_hosts == self.cgiurl_services == None:
                    # services (unknown, warning or critical?)
                    self.cgiurl_services = {'hard': self.monitor_cgi_url + '/monitoring/list/services?service_state>0&service_state<=3&service_state_type=1&addColumns=service_last_check&format=json',\
                                            'soft': self.monitor_cgi_url + '/monitoring/list/services?service_state>0&service_state<=3&service_state_type=0&addColumns=service_last_check&format=json'}
                    # hosts (up or down or unreachable)
                    self.cgiurl_hosts = {'hard': self.monitor_cgi_url + '/monitoring/list/hosts?host_state>0&host_state<=2&host_state_type=1&addColumns=host_last_check&format=json',\
                                         'soft': self.monitor_cgi_url + '/monitoring/list/hosts?host_state>0&host_state<=2&host_state_type=0&addColumns=host_last_check&format=json'}
                self._get_status_JSON()
            else:
                # error result in case version still was ''
                return result
        except:
            # set checking flag back to False
            self.isChecking = False
            result, error = self.Error(sys.exc_info())
            return Result(result=result, error=error)

        #dummy return in case all is OK
        return Result()


    def _get_status_JSON(self):
        """
            Get status from Icinga Server - the JSON way
        """
        # new_hosts dictionary
        self.new_hosts = dict()

        # hosts - mostly the down ones
        # now using JSON output from Icinga
        try:
            for status_type in 'hard', 'soft':
                result = self.FetchURL(self.cgiurl_hosts[status_type], giveback='raw')
                # purify JSON result of unnecessary control sequence \n
                jsonraw, error = copy.deepcopy(result.result.replace('\n', '')), copy.deepcopy(result.error)

                if error != '': return Result(result=jsonraw, error=error)

                hosts = copy.deepcopy(json.loads(jsonraw))

                for host in hosts:
                    # make dict of tuples for better reading
                    h = dict(host.items())

                    # host
                    if self.use_display_name_host == False:
                        # according to http://sourceforge.net/p/nagstamon/bugs/83/ it might
                        # better be host_name instead of host_display_name
                        # legacy Icinga adjustments
                        if 'host_name' in h: host_name = h['host_name']
                        elif 'host' in h: host_name = h['host']
                    else:
                        # https://github.com/HenriWahl/Nagstamon/issues/46 on the other hand has
                        # problems with that so here we go with extra display_name option
                        host_name = h['host_display_name']

                    # host objects contain service objects
                    if not host_name in self.new_hosts:
                        self.new_hosts[host_name] = GenericHost()
                        self.new_hosts[host_name].name = host_name
                        self.new_hosts[host_name].server = self.name
                        self.new_hosts[host_name].status = self.STATES_MAPPING['hosts'][h['host_state']]
                        self.new_hosts[host_name].last_check = datetime.datetime.utcfromtimestamp(int(h['host_last_check']))
                        duration=datetime.datetime.now()-datetime.datetime.utcfromtimestamp(int(h['host_last_state_change']))
                        self.new_hosts[host_name].duration = strfdelta(duration, '{days}d {hours}h {minutes}m {seconds}s')
                        self.new_hosts[host_name].attempt = h['host_attempt']
                        self.new_hosts[host_name].status_information= h['host_output'].replace('\n', ' ').strip()
                        self.new_hosts[host_name].passiveonly = not(h['host_active_checks_enabled'])
                        self.new_hosts[host_name].notifications_disabled = not(h['host_notifications_enabled'])
                        self.new_hosts[host_name].flapping = h['host_is_flapping']
                        self.new_hosts[host_name].acknowledged = h['host_acknowledged']
                        self.new_hosts[host_name].scheduled_downtime = h['host_in_downtime']
                        self.new_hosts[host_name].status_type = status_type
                    del h, host_name
        except:


            import traceback
            traceback.print_exc(file=sys.stdout)

            # set checking flag back to False
            self.isChecking = False
            result, error = self.Error(sys.exc_info())
            return Result(result=result, error=error)

        # services
        try:
            for status_type in 'hard', 'soft':
                result = self.FetchURL(self.cgiurl_services[status_type], giveback='raw')
                # purify JSON result of unnecessary control sequence \n
                jsonraw, error = copy.deepcopy(result.result.replace('\n', '')), copy.deepcopy(result.error)

                if error != '': return Result(result=jsonraw, error=error)


                services = copy.deepcopy(json.loads(jsonraw))


                for service in services:
                    # make dict of tuples for better reading
                    s = dict(service.items())

                    if self.use_display_name_host == False:
                        # according to http://sourceforge.net/p/nagstamon/bugs/83/ it might
                        # better be host_name instead of host_display_name
                        # legacy Icinga adjustments
                        if 'host_name' in s: host_name = s['host_name']
                        elif 'host' in s: host_name = s['host']
                    else:
                        # https://github.com/HenriWahl/Nagstamon/issues/46 on the other hand has
                        # problems with that so here we go with extra display_name option
                        host_name = s['host_display_name']

                    # host objects contain service objects
                    ###if not self.new_hosts.has_key(host_name):
                    if not host_name in self.new_hosts:
                        self.new_hosts[host_name] = GenericHost()
                        self.new_hosts[host_name].name = host_name
                        self.new_hosts[host_name].status = 'UP'

                    if self.use_display_name_host == False:
                        # legacy Icinga adjustments
                        if 'service_description' in s: service_name = s['service_description']
                        elif 'description' in s: service_name = s['description']
                        elif 'service' in s: service_name = s['service']
                    else:
                        service_name = s['service_display_name']

                    # if a service does not exist create its object
                    if not service_name in self.new_hosts[host_name].services:
                        self.new_hosts[host_name].services[service_name] = GenericService()
                        self.new_hosts[host_name].services[service_name].host = host_name
                        self.new_hosts[host_name].services[service_name].name = service_name
                        self.new_hosts[host_name].services[service_name].server = self.name
                        self.new_hosts[host_name].services[service_name].status = self.STATES_MAPPING['services'][s['service_state']]
                        self.new_hosts[host_name].services[service_name].last_check = datetime.datetime.utcfromtimestamp(int(s['service_last_check']))
                        duration=datetime.datetime.now()-datetime.datetime.utcfromtimestamp(int(s['service_last_state_change']))
                        self.new_hosts[host_name].services[service_name].duration = strfdelta(duration, '{days}d {hours}h {minutes}m {seconds}s')
                        self.new_hosts[host_name].services[service_name].attempt = s['service_attempt']
                        self.new_hosts[host_name].services[service_name].status_information = s['service_output'].replace('\n', ' ').strip()
                        self.new_hosts[host_name].services[service_name].passiveonly = not(s['service_active_checks_enabled'])
                        self.new_hosts[host_name].services[service_name].notifications_disabled = not(s['service_notifications_enabled'])
                        self.new_hosts[host_name].services[service_name].flapping = s['service_is_flapping']
                        self.new_hosts[host_name].services[service_name].acknowledged = s['service_acknowledged']
                        self.new_hosts[host_name].services[service_name].scheduled_downtime = s['service_in_downtime']

                        self.new_hosts[host_name].services[service_name].status_type = status_type
                    del s, host_name, service_name
        except:

            import traceback
            traceback.print_exc(file=sys.stdout)

            # set checking flag back to False
            self.isChecking = False
            result, error = self.Error(sys.exc_info())
            return Result(result=result, error=error)

        # some cleanup
        del jsonraw, error, hosts, services

        #dummy return in case all is OK
        return Result()




    def _set_recheck(self, host, service):
        # First retrieve the info page for this host/service
        if service=='':
            url=self.monitor_cgi_url+'/monitoring/host/show?host='+host
        else:
            url=self.monitor_cgi_url+'/monitoring/service/show?host='+host+'&service='+service
        result = self.FetchURL(url, giveback='raw')

        if result.error != '':
            return result
        else:
            pageraw = result.result

        pagesoup = BeautifulSoup(pageraw, 'html.parser')

        # Extract the relevant form element values

        formtag=pagesoup.find('form',{'name':'IcingaModuleMonitoringFormsCommandObjectCheckNowCommandForm'})
        CSRFToken=formtag.findNext('input',{'name':'CSRFToken'})['value']
        formUID=formtag.findNext('input',{'name':'formUID'})['value']
        btn_submit=formtag.findNext('button',{'name':'btn_submit'})['value']

        # Pass these values to the same URL as cgi_data
        cgi_data={}
        cgi_data['CSRFToken']=CSRFToken
        cgi_data['formUID']=formUID
        cgi_data['btn_submit']=btn_submit
        result = self.FetchURL(url, giveback='raw',cgi_data=cgi_data)


    def _set_acknowledge(self, host, service, author, comment, sticky, notify, persistent, all_services=[]):
        # First retrieve the info page for this host/service
        if service=='':
            url=self.monitor_cgi_url+'/monitoring/service/acknowledge-problem?host='+host
        else:
            url=self.monitor_cgi_url+'/monitoring/service/acknowledge-problem?host='+host+'&service='+service

        result = self.FetchURL(url, giveback='raw')

        if result.error != '':
            return result
        else:
            pageraw = result.result

        pagesoup = BeautifulSoup(pageraw, 'html.parser')

        # Extract the relevant form element values

        formtag=pagesoup.find('form',{'name':'IcingaModuleMonitoringFormsCommandObjectAcknowledgeProblemCommandForm'})
        CSRFToken=formtag.findNext('input',{'name':'CSRFToken'})['value']
        formUID=formtag.findNext('input',{'name':'formUID'})['value']
        btn_submit=formtag.findNext('input',{'name':'btn_submit'})['value']

        # Pass these values to the same URL as cgi_data
        cgi_data={}
        cgi_data['CSRFToken']=CSRFToken
        cgi_data['formUID']=formUID
        cgi_data['btn_submit']=btn_submit
#
        cgi_data['comment']=comment
        cgi_data['persistent']=int(persistent)
        cgi_data['sticky']=int(sticky)
        cgi_data['notify']=int(notify)
        cgi_data['comment']=comment

        self.FetchURL(url, giveback='raw',cgi_data=cgi_data)

        if len(all_services) > 0:
            for s in all_services:
                #cheap, recursive solution...
                self._set_acknowledge(host, s, author, comment, sticky, notify, persistent, [])

    def _set_submit_check_result(self, host, service, state, comment, check_output, performance_data):
        # First retrieve the info page for this host/service
        if service=='':
            url=self.monitor_cgi_url+'/monitoring/host/process-check-result?host='+host
            status=self.STATES_MAPPING_REV['hosts'][state.upper()]
        else:
            url=self.monitor_cgi_url+'/monitoring/service/process-check-result?host='+host+'&service='+service
            status=self.STATES_MAPPING_REV['services'][state.upper()]

        result = self.FetchURL(url, giveback='raw')

        if result.error != '':
            return result
        else:
            pageraw = result.result

        pagesoup = BeautifulSoup(pageraw, 'html.parser')

        # Extract the relevant form element values

        formtag=pagesoup.find('form',{'name':'IcingaModuleMonitoringFormsCommandObjectProcessCheckResultCommandForm'})
        CSRFToken=formtag.findNext('input',{'name':'CSRFToken'})['value']
        formUID=formtag.findNext('input',{'name':'formUID'})['value']
        btn_submit=formtag.findNext('input',{'name':'btn_submit'})['value']

        # Pass these values to the same URL as cgi_data
        cgi_data={}
        cgi_data['CSRFToken']=CSRFToken
        cgi_data['formUID']=formUID
        cgi_data['btn_submit']=btn_submit

        cgi_data['status']=status
        cgi_data['output']=check_output
        cgi_data['perfdata']=performance_data

        self.FetchURL(url, giveback='raw',cgi_data=cgi_data)

    def _set_downtime(self, host, service, author, comment, fixed, start_time, end_time, hours, minutes):
        # First retrieve the info page for this host/service
        if service=='':
            url=self.monitor_cgi_url+'/monitoring/host/schedule-downtime?host='+host
        else:
            url=self.monitor_cgi_url+'/monitoring/service/schedule-downtime?host='+host+'&service='+service

        result = self.FetchURL(url, giveback='raw')

        if result.error != '':
            return result
        else:
            pageraw = result.result

        pagesoup = BeautifulSoup(pageraw, 'html.parser')

        # Extract the relevant form element values

        formtag=pagesoup.find('form',{'name':'IcingaModuleMonitoringFormsCommandObjectScheduleServiceDowntimeCommandForm'})
        CSRFToken=formtag.findNext('input',{'name':'CSRFToken'})['value']
        formUID=formtag.findNext('input',{'name':'formUID'})['value']
        btn_submit=formtag.findNext('input',{'name':'btn_submit'})['value']

        # Pass these values to the same URL as cgi_data
        cgi_data={}
        cgi_data['CSRFToken']=CSRFToken
        cgi_data['formUID']=formUID
        cgi_data['btn_submit']=btn_submit
        cgi_data['comment']=comment
        if fixed:
           cgi_data['type']='fixed'
        else:
            cgi_data['type']='flexible'
            cgi_data['hours']=hours
            cgi_data['minutes']=minutes
        #TODO: start_time and end_time format is unknown/free text
        if start_time=='' or start_time=='n/a':
            start=datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
        else:
            start=start_time
        if end_time=='' or end_time=='n/a':
            end=(datetime.datetime.now() + datetime.timedelta(hours=hours, minutes=minutes)).strftime('%Y-%m-%dT%H:%M:%S')
        else:
            end=end_time
        cgi_data['start']=start
        cgi_data['end']=end


        self.FetchURL(url, giveback='raw', cgi_data=cgi_data)