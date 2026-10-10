"""Windows read-only runtime inspection. Layout verified on CN 6.5.10; fail closed on mismatch."""
STOP=None
import ctypes as c
from ctypes import wintypes as w
import socket,json,struct,time,re,collections
k=c.WinDLL('kernel32',use_last_error=True)
def bind(name,args,ret):
 f=getattr(k,name);f.argtypes=args;f.restype=ret;return f
op=bind('OpenProcess',[w.DWORD,w.BOOL,w.DWORD],w.HANDLE)
close=bind('CloseHandle',[w.HANDLE],w.BOOL)
qp=bind('QueryFullProcessImageNameW',[w.HANDLE,w.DWORD,w.LPWSTR,c.POINTER(w.DWORD)],w.BOOL)
rpm=bind('ReadProcessMemory',[w.HANDLE,c.c_void_p,c.c_void_p,c.c_size_t,c.POINTER(c.c_size_t)],w.BOOL)
class MBI(c.Structure):
 _fields_=[('BaseAddress',c.c_void_p),('AllocationBase',c.c_void_p),('AllocationProtect',w.DWORD),('PartitionId',w.WORD),('RegionSize',c.c_size_t),('State',w.DWORD),('Protect',w.DWORD),('Type',w.DWORD)]
vq=bind('VirtualQueryEx',[w.HANDLE,c.c_void_p,c.POINTER(MBI),c.c_size_t],c.c_size_t)
h=None

def open_process(pid):
 global h
 h=op(0x410,False,pid)
 if not h:raise PermissionError('无法只读访问游戏，请关闭挂件并使用“启动内存挂件.bat”以管理员运行')
 b=c.create_unicode_buffer(32768);n=w.DWORD(len(b))
 if not qp(h,0,b,c.byref(n)) or b.value.lower().rsplit(chr(92), 1)[-1] != 'heavenburnsred.exe':
  close(h);h=None;raise RuntimeError('目标不是 HeavenBurnsRed.exe')

def read(addr,n):
 if not 65536<addr<0x7fffffffffff:return b''
 b=c.create_string_buffer(n);got=c.c_size_t()
 if not rpm(h,addr,b,n,c.byref(got)):return b''
 return b.raw[:got.value]
def q(addr):
 b=read(addr,8);return struct.unpack('<Q',b)[0] if len(b)==8 else 0
def i(addr):
 b=read(addr,4);return struct.unpack('<i',b)[0] if len(b)==4 else None
def name(addr):
 b=read(addr,120).split(b'\0')[0]
 try:s=b.decode('ascii')
 except:return None
 return s if re.fullmatch(r'[A-Za-z_<][A-Za-z0-9_<>.,+:`]{0,118}',s) else None
def cls(obj):return name(q(q(obj)+16))
def string(obj):
 if cls(obj)!='String':return None
 n=i(obj+16)
 if n is None or not 0<=n<=250:return None
 try:return read(obj+20,n*2).decode('utf-16-le')
 except:return None
def fields(obj):
 out=[];klass=q(obj)
 for level in range(6):
  cn=name(q(klass+16))
  if cn in (None,'Object'):break
  fp=q(klass+128)
  for j in range(160):
   b=read(fp+j*32,32)
   if len(b)!=32:break
   np,tp,parent,offset,token=struct.unpack('<QQQiI',b);fn=name(np)
   if parent!=klass or not fn:break
   bits=i(tp+8) or 0;kind=(bits>>16)&255
   if bits&16 or not 0<=offset<8192:continue
   row={'name':fn,'offset':offset,'type':kind,'declared':cn}
   fmt={2:'<?',8:'<i',9:'<I',10:'<q',11:'<Q',12:'<f',13:'<d'}.get(kind)
   if fmt:
    data=read(obj+offset,struct.calcsize(fmt))
    if len(data)==struct.calcsize(fmt):row['value']=struct.unpack(fmt,data)[0]
   else:
    ptr=q(obj+offset);text=string(ptr);oc=cls(ptr)
    if text is not None:row['text']=text
    elif oc:row['object']={'address':hex(ptr),'class':oc}
   out.append(row)
  klass=q(klass+88)
 return {'address':hex(obj),'class':cls(obj),'fields':out}
def scan(pattern,seconds=35,cap=5000):
 address=0;started=time.monotonic();hits=[];total=0;failed=0;tail=b''
 patterns=pattern if isinstance(pattern, tuple) else (pattern,)
 width=max(map(len,patterns));matcher=re.compile(b'|'.join(re.escape(p) for p in patterns)) if len(patterns)>1 else None
 while address<0x7fffffffffff:
  m=MBI()
  if not vq(h,address,c.byref(m),c.sizeof(m)):break
  base=m.BaseAddress or 0;size=m.RegionSize
  if m.State==0x1000 and m.Type==0x20000 and m.Protect==4:
   pos=base;tail=b''
   while pos<base+size:
    if time.monotonic()-started>seconds:raise TimeoutError('内存定位超时，请停止后重试')
    if STOP is not None and STOP.is_set():raise InterruptedError('已停止')
    n=min(1048576,base+size-pos);data=read(pos,n)
    if data:
     total+=len(data);data=tail+data;origin=pos-len(tail)
     matches=(m.start() for m in matcher.finditer(data)) if matcher else (m.start() for m in re.finditer(re.escape(patterns[0]),data))
     for ix in matches:
      addr=origin+ix
      if addr+width>pos:
       if len(hits)>=cap:raise RuntimeError('定位候选过多，已停止')
       hits.append(addr)
     tail=data[-(width-1):]
    else:failed+=1;tail=b''
    pos+=n
  if not size:break
  address=base+size
 return hits,{'limited':False,'bytes':total,'failed':failed,'seconds':round(time.monotonic()-started,2)}
