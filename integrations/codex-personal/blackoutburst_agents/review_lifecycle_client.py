"""Narrow explicit controller channel for claim and review-only publication."""
from copy import deepcopy

from .board_reader import BoardReadError, _text
from .review_client import DeliveryReviewBoardClient
from .worker_client import _arguments


class DeliveryReviewLifecycleClient(DeliveryReviewBoardClient):
    def set_effect_guard(self,guard):
        if not callable(guard):raise BoardReadError('review_controller_guard_required')
        self._effect_guard=guard

    def _before_effect(self):
        guard=getattr(self,'_effect_guard',None)
        if not callable(guard):raise BoardReadError('review_controller_guard_required')
        guard()

    def register_initial(self,*,skills):
        self._before_effect()
        self._initialize()
        self._before_effect()
        return super().register_initial(skills=skills)

    def call(self, name, args):
        if name in ('get_ticket','get_thread','get_message'):
            return super().call(name,args)
        if not self._registered:raise BoardReadError('reviewer_registration_required')
        if name=='get_work':
            if (type(args) is not dict or set(args)!={'project_id','limit'}
                    or not _text(args['project_id'],200) or type(args['limit']) is not int or args['limit']!=1):
                raise BoardReadError('invalid_arguments')
            values=deepcopy(args)
        elif name=='list_activity':
            if (type(args) is not dict or set(args)!={'ticket_id','after','limit','order'}
                    or not _text(args['ticket_id'],200) or type(args['after']) is not int or args['after']<0
                    or type(args['limit']) is not int or not 1<=args['limit']<=10 or args['order'] not in ('asc','desc')):
                raise BoardReadError('invalid_arguments')
            values=deepcopy(args)
        elif name in ('claim_ticket','update_ticket'):
            values=_arguments(name,args)
            if name=='update_ticket':
                changes=values['changes']
                if (set(changes)!={'status','outcome','evidence','release_claim'}
                        or changes['status']!='review' or changes['release_claim'] is not True):
                    raise BoardReadError('review_publication_only')
        else:raise BoardReadError('review_controller_tool_not_allowed')
        self._initialize()
        if name in ('claim_ticket','update_ticket'):self._before_effect()
        return self._tool(name,{**values,'session_id':self.session_id})
