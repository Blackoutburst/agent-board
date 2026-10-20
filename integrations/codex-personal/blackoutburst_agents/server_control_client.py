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
            if name in ('publish_server_catalog','poll_instance_commands'):
                if result['host_id']!=args['host_id']:raise HostError('managed_protocol_invalid')
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
