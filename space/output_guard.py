"""Deterministic checks first. Never replace names inside arbitrary quotations."""
import re

def inspect_output(text,user_text='',reason=None):
    text=text.strip();flags=[];changes=[]
    fiction=bool(re.search(r'扮演|台词|小说|角色|剧本|接一句|接话|跑团|翻译|引用',user_text))
    repeat_requested=bool(re.search(r'重复|复读|逐字|原样|背诵|代码',user_text))
    if not repeat_requested:
        # Only identical adjacent full paragraphs, never a general paraphrase or name rewrite.
        parts=text.split('\n\n');clean=[]
        for part in parts:
            if clean and len(part)>12 and part==clean[-1]:changes.append('removed_exact_adjacent_duplicate_paragraph')
            else:clean.append(part)
        text='\n\n'.join(clean)
        sentences=[s.strip() for s in re.findall(r'[^。！？!?\n]+[。！？!?]',text)]
        if any(len(s)>=12 and sentences.count(s)>=3 for s in sentences):flags.append('repetition_loop')
    if not fiction:
        plain=re.sub(r'```[\s\S]*?```|[“「][^”」]*[”」]','',text)
        if re.search(r'(?:我小时候|我(?:曾经|当年)在.{0,15}(?:上学|读书)|我昨天(?:吃了|见到了)|我的童年)',plain):flags.append('unsupported_autobiography')
    kind=(reason or {}).get('kind','') if isinstance(reason,dict) else ''
    if re.search(r'token|length|limit|truncat',kind,re.I):flags.append('provider_output_limit')
    if not text:flags.append('empty_reply')
    return {'text':text,'flags':list(dict.fromkeys(flags)),'local_changes':list(dict.fromkeys(changes)),'needs_repair':bool(flags)}

def repair_instruction(question,flags):
    return '后台文字校正：上一条回答触发了这些明确问题：'+', '.join(flags)+'。只修正这些问题，重新回答下面原消息，保持小鲸鱼身份和当前侧重。不编造经历，不同义绕圈，结束于完整句子。不要重做任何工具操作，不输出校正说明。\n【原消息】\n'+question
