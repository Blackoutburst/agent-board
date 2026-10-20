"""Device-only control channel. Never registers a model session."""
from .board_reader import BoardReader,BoardReadError
from .host_config import HostError
from .server_control_protocol import arguments,response


class ServerControlClient(BoardReader):
    def register(self):
        raise BoardReadError('managed_registration_forbidden')

    def call(self,name,args):
        try:arguments(name,args)
        except HostError:raise BoardReadError('managed_protocol_invalid') from None
        self._initialize()
        result=self._tool(name,args)
        try:
            response(name,result)
            if name in ('publish_server_catalog','replace_server_catalog','poll_instance_commands'):
                if result['host_id']!=args['host_id']:raise HostError('managed_protocol_invalid')
                if name=='replace_server_catalog':
                    entries={e['catalog_key']:e for e in args['entries']}
                    if (result['catalog_version']!=args['expected_catalog_version']+1
                            or {i['catalog_key'] for i in result['instances']}!=set(entries)):
                        raise HostError('managed_protocol_invalid')
                    for i in result['instances']:
                        c=i['current_command']
                        if (any(i[k]!=entries[i['catalog_key']][k] for k in ('profile_id','profile_version','mode','launchspec_sha256'))
                                or not i['active'] or i['desired_state']!='stopped' or i['observed_state']!='unknown'
                                or i['cleanup_confirmed'] or c is None or c['action']!='stop' or c['status']!='complete'
                                or c['budget']!=0 or c['instance_version']!=i['version']-1
                                or c['launchspec_sha256']==i['launchspec_sha256']):raise HostError('managed_protocol_invalid')
            else:
                i=result['instance'];c=result['command']
                if i['host_id']!=args['host_id']:raise HostError('managed_protocol_invalid')
                if name=='ack_instance_command':
                    if (c is None or c['id']!=args['command_id'] or c['status']!='accepted'
                            or c['revision']!=args['expected_revision']+1):raise HostError('managed_protocol_invalid')
                elif (any(i[k]!=args[k] for k in ('generation','launchspec_sha256','command_id'))
                        or i['id']!=args['instance_id'] or i['observed_state']!=args['state']
                        or i['cleanup_confirmed']!=args['cleanup_confirmed'] or i['reason']!=args['reason']
                        or i['observation_revision']!=args['expected_observation_revision']+1):
                    raise HostError('managed_protocol_invalid')
            return result
        except HostError:raise BoardReadError('managed_protocol_invalid') from None
