"""Space-owned MCP definitions around the fixed phone adapter."""
import base64,json,os,threading,time
from space_tools import spec
N={'type':'integer','minimum':0}
TOOLS=[
 spec('phone_health','核实自己的固定手机是否开机完成。'),
 spec('phone_ui','读取自己手机当前可见文字及可交互节点坐标；省略无内容的容器。不能推测屏幕外内容；界面树超时或不可用时改用phone_screen观察，不连续重试界面树。'),
 spec('phone_screen','观察自己手机的真实截图，同时为主人保存预览；可结合phone_ui核对文字与坐标。'),
 spec('phone_click','点击手机当前界面中已观察到的像素坐标。回执直接附动作后约半秒的真实画面，不必再单独调用phone_screen。',{'x':N,'y':N},['x','y']),
 spec('phone_type','向手机当前输入框输入文本；保留原输入法。不得未经授权发送消息。回执直接附动作后约半秒的真实画面，不必再单独调用phone_screen。',{'text':{'type':'string','maxLength':6000}},['text']),
 spec('phone_key','按自己手机的系统或导航键。SCREENSHOT=系统截屏：截图存进手机相册，回执给出已核实的文件路径；需要截屏时只用它一次，不要用音量键组合、三指手势或反复点击（这些做不到）。只是想看画面用phone_screen。其他按键的回执直接附按键后的真实画面，不必再单独调用phone_screen。',{'key':{'type':'string','enum':['HOME','BACK','APP_SWITCH','ENTER','TAB','BACKSPACE','ESC','DPAD_UP','DPAD_DOWN','DPAD_LEFT','DPAD_RIGHT','DPAD_CENTER','SCREENSHOT']}},['key']),
 spec('phone_swipe','在已观察到的手机屏幕内滑动。回执直接附动作后约半秒的真实画面，不必再单独调用phone_screen。',{'x1':N,'y1':N,'x2':N,'y2':N,'duration_ms':{'type':'integer','minimum':100,'maximum':2000}},['x1','y1','x2','y2'])
]

# 主人代操作识别：她的 adb 点击/输入不产生 Windows 键鼠输入，所以“宿主有新的键鼠输入、
# 且前台窗口是这台模拟器”只可能是主人在模拟器窗口里操作。每秒采样一次，只记时间，
# 不记按键内容；每次手机工具回执附上“自你上一步以来主人是否动过手机”。
EMULATOR_TITLE='Android Emulator - DSH_Phone_CN'
OWNER={'last':None,'samples':0,'watching':False}
LAST_ACTION={}
_owner_lock=threading.Lock()

def _host_input_tick():
    try:
        import ctypes
        class LASTINPUT(ctypes.Structure):_fields_=[('cbSize',ctypes.c_uint),('dwTime',ctypes.c_uint)]
        info=LASTINPUT(ctypes.sizeof(LASTINPUT),0)
        return info.dwTime if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)) else None
    except (AttributeError,OSError):return None

def _foreground_title():
    try:
        import ctypes
        user32=ctypes.windll.user32;buf=ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(user32.GetForegroundWindow(),buf,512)
        return buf.value
    except (AttributeError,OSError):return ''

def sample_owner_input(previous_tick):
    """One watcher step; returns the tick to compare against next time."""
    tick=_host_input_tick()
    if tick is not None and previous_tick is not None and tick!=previous_tick and _foreground_title().startswith(EMULATOR_TITLE):
        with _owner_lock:OWNER['last']=time.monotonic();OWNER['samples']+=1
    return tick

def watch_owner_input(interval=1.0):
    OWNER['watching']=True;tick=_host_input_tick()
    while True:
        time.sleep(interval)
        try:tick=sample_owner_input(tick)
        except Exception:pass

def owner_activity(since):
    """since: monotonic end of this persona's previous phone action, or None."""
    with _owner_lock:last=OWNER['last']
    now=time.monotonic()
    # 本进程里她还没用过手机时，只认最近两分钟内的主人操作，不把几小时前的输入算进来。
    value={'watching':OWNER['watching'],'ownerInputOnEmulator':last is not None and (last>since if since is not None else now-last<120)}
    if last is not None:value['lastOwnerInputSecondsAgo']=round(now-last,1)
    if value['ownerInputOnEmulator']:
        value['note']=('主人'+('在你上一步手机操作之后' if since is not None else '最近')+'直接在模拟器窗口里用键盘或鼠标操作过手机（最近一次约'
            +str(round(now-last))+'秒前）。这段时间画面上的变化可能是主人做的，不是你的操作结果，也不是你的失误；'
            '不要把主人的操作记成自己做过的事。不确定时先问主人，不要撤销主人的操作。')
    return value

def compact_ui(result):
    """Keep observable text and interaction affordances without empty containers."""
    nodes=[]
    for node in result.get('nodes',[]):
        if not (node.get('text') or node.get('contentDescription') or
                any(node.get(k) for k in ('clickable','scrollable','focusable','focused')) or
                'EditText' in node.get('className','')):
            continue
        keep={k:v for k,v in node.items() if v not in ('',False,None)}
        keep['nodeId']=node['nodeId']
        if node.get('enabled') is False:keep['enabled']=False
        nodes.append(keep)
    return {**result,'nodes':nodes,'nodeCount':len(nodes),
            'sourceNodeCount':len(result.get('nodes',[])),
            'representation':'visible-text-and-interactive-nodes'}

def screen_after_seconds(config,device):
    """主人 2026-10-24 提速：动作回执直接附动作后的画面；config.speed 可关闭或调等待时间。"""
    speed=config.get('speed') or {}
    if speed.get('screen_after_action',True) is not True:return None
    value=speed.get(device+'_settle_ms',500 if device=='phone' else 400)
    if type(value) not in (int,float) or not 0<=value<=3000:value=500 if device=='phone' else 400
    return value/1000

def invoke(store,name,args):
    if os.name != 'nt':
        # 这个模块的 phone_* 适配器只对应 Windows 模拟器；Linux 空间
        # 没有 phone.py，真正可用的是 her_android 的 android_*。
        raise RuntimeError('Linux 空间没有 phone_* 工具；请使用 android_*（android_view/android_info/android_ui/android_tap/android_swipe/android_type/android_key/android_app）')
    import phone
    action=name.removeprefix('phone_')
    actor=store.identity.actor();activity=owner_activity(LAST_ACTION.get(actor))
    try:
        after=screen_after_seconds(getattr(store,'config',{}),'phone')
        result=phone.call(store.root,action,**args,**({'screen_after':after} if after is not None and action in ('click','swipe','key','type') else {}))
    except phone.PhoneError as error:
        task_id=(store.get('active_request') or {}).get('task_id')
        store.event('phone.failed',error.result,task_id,store.identity.runtime()['session_id'])
        if error.result.get('uncertain'):
            # 主人要求：ADB 偶发超时不再触发全局暂停/取消/任务不确定。
            # 只把不确定回执交回给她，由她看画面确认后自行决定是否重试。
            store.event('phone.uncertain-continue',{'message':'动作回执不确定，但按主人设置不再自动暂停；先看画面确认实际状态，需要时自行重试。','code':error.result.get('code'),'action':name},task_id,store.identity.runtime()['session_id'])
        raise
    finally:
        LAST_ACTION[actor]=time.monotonic()
    result['ownerActivity']=activity
    if activity['ownerInputOnEmulator']:
        store.event('phone.owner_input',{'action':name,'lastOwnerInputSecondsAgo':activity.get('lastOwnerInputSecondsAgo'),
            'message':'主人在她上一步之后直接操作过模拟器；已在回执里告诉她这些变化不是她做的。'},
            (store.get('active_request') or {}).get('task_id'),store.identity.runtime()['session_id'])
    if action=='ui':result=compact_ui(result)
    if result.get('image'):
        raw=base64.b64decode(result['image'],validate=True)
        folder=store.data/'phone-frames'/store.identity.actor();folder.mkdir(parents=True,exist_ok=True)
        tmp=folder/'latest.tmp';tmp.write_bytes(raw);tmp.replace(folder/'latest.png')
        result.update(preview='/api/phone-frame',modelImageAttached=True)
    if action=='health':store.phone_live=result
    store.identity.log('phone.'+action,'已取得手机'+action+'回执；画面与输入内容保留在当前模式。',store.get('active_request').get('task_id'),store.identity.runtime()['session_id'])
    return result

def poll_once(store):
    from device_lease import background_lease
    # 她正在用手机时，每次动作本身就核对了连接；后台体检只会和她抢设备锁。
    if LAST_ACTION and time.monotonic()-max(LAST_ACTION.values())<30:return False
    if os.name != 'nt':
        # Linux 空间没有 Windows ADB；手机实际由 .204 的 her-android 服务连接。
        from her_android import health as android_health
        with background_lease(store,'phone') as admitted:
            if not admitted:return False
            try:store.phone_live=android_health(store)
            except Exception as error:store.phone_live={'ready':False,'error':str(error)[:200]}
        return True
    import phone
    with background_lease(store,'phone') as admitted:
        if not admitted:return False
        try:store.phone_live=phone.health(store.root)
        except Exception as error:store.phone_live={'ready':False,'error':str(error)[:200]}
    return True

def start(store):
    def poll():
        while True:
            poll_once(store)
            time.sleep(10)
    threading.Thread(target=poll,daemon=True,name='space-phone-status').start()
    threading.Thread(target=watch_owner_input,daemon=True,name='space-phone-owner-input').start()
