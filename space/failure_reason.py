"""Publish classified explanations; raw provider errors stay in native records."""
def classify(reason, stop_source=None):
    kind = (reason or {}).get('kind', 'unknown')
    error = (reason or {}).get('error') or {}
    raw = str(error.get('message', ''))
    if error.get('status') == 402 or 'Insufficient Balance' in raw:
        code, message = 'PROVIDER_BALANCE_EMPTY', 'DeepSeek 账户余额不足，本轮未生成回复。请在官方账户补充余额或在本机更新可用密钥后提交新任务；旧任务不会重发。'
    elif '工作上下文达到上限' in raw or 'SPACE_CONTEXT_LIMIT' in raw:
        code, message = 'CONTEXT_LIMIT', '本轮工作上下文已满，原记录已保留。下一条新任务会整理接续；旧任务不会重发。'
    elif 'REPEATED_ACTION_LIMIT' in raw:
        code, message = 'REPEATED_ACTION_LIMIT', '同一行动已连续执行30次，第31次未执行，本轮已停止并保留进度。正常变化的行动不受总轮数限制。'
    elif '本轮达到' in raw and '保留进度并等待主人' in raw:
        code, message = 'STEP_LIMIT', '本轮达到步数保护，已停止并保留阶段回执。可安排新的核对或后续任务，已完成的动作不会自动重做。'
    elif stop_source == 'observation-unavailable' or '观察链路' in raw:
        code, message = 'OBSERVATION_UNAVAILABLE', '观察连接暂时不可用，本轮已停止。记录已保留，连接恢复后可提交新任务。'
    elif 'BUDGET' in raw or '预算' in raw or '预留' in raw:
        code, message = 'BUDGET_BLOCKED', '预算检查未通过，本轮停止。已保留账本和未知费用，请核对预算回执。'
    elif stop_source == 'owner-message-preempt':
        code, message = 'OWNER_MESSAGE_FIRST', '主人发来新消息，自主探索先停下，马上处理主人的消息。'
    elif stop_source == 'owner-question-timeout':
        code, message = 'OWNER_QUESTION_TIMEOUT', '问题已经放到「需要你决定」；30 秒内没有收到回答，本轮先结束。你回答后她会接着做，旧任务不会重发。'
    elif stop_source == 'owner-question-answered':
        code, message = 'OWNER_QUESTION_ANSWERED', '你已回答这个问题，本轮结束；回答已作为新任务交给她继续。'
    elif stop_source in ('owner-correction-or-pause', 'host-became-active') or kind == 'cancelled':
        code, message = 'CANCELLED', '本轮已按停止条件结束，未自动重发。'
    elif kind == 'completed':
        return None
    else:
        code, message = 'RUNTIME_STOPPED', '本轮未能正常完成，执行记录已保留。旧任务不会自动重发。'
    return {'code': code, 'message': message}
