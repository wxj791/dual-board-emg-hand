"""Offline A package/capture tools and explicit future port access.

Only `port` opens serial. No programming or servo-control operations exist.
"""
import argparse,json,re,struct,time
from pathlib import Path
from a_ab_v2_cli import metadata,transaction_crc,validate,request,parse_frames,commands

def load_package(path,capacity=32):
    p=json.loads(Path(path).read_text(encoding='utf8'))
    if p['format']!='EMG-A-AB-V2-1':raise ValueError('unsupported model format')
    validate(p['banks'],p['active_classes'],capacity)
    crc=transaction_crc(metadata(p['active_classes']),p['banks'])
    if int(p['transaction_crc32'],16)!=crc:raise ValueError('model transaction CRC mismatch')
    p['capacity_checked']=capacity
    return p

def status_identity(status):
    """Decode the candidate's canonical identity; missing CRC is unconfirmed."""
    for key in ['valid','loading','model_crc32_valid']:
        if type(status.get(key)) is not int or status[key] not in (0,1):
            raise ValueError('missing or invalid '+key)
    for key,width in [('active_hex',4),('generation_hex',4),('model_crc32_hex',8)]:
        value=status.get(key)
        if not isinstance(value,str) or re.fullmatch('[0-9a-f]{'+str(width)+'}',value) is None:
            raise ValueError('missing or invalid '+key)
    if not 2<=int(status['active_hex'],16)<=32:
        raise ValueError('active class count outside 2..32')
    if status['valid']!=status['model_crc32_valid'] or (status['loading'] and status['valid']):
        raise ValueError('inconsistent model validity/loading/CRC validity')
    return status

def verify_deployment(package,old,new):
    """Require an actual verified transaction identity before reporting success."""
    try:
        status_identity(new)
        count=int(new['active_hex'],16);generation=int(new['generation_hex'],16)
        old_generation=int(old['generation_hex'],16)
        crc=new['model_crc32_hex']
        if (new['valid']!=1 or new['loading']!=0 or count!=package['active_classes'] or
            generation!=((old_generation+1)&65535) or new['model_crc32_valid']!=1 or
            not isinstance(crc,str) or re.fullmatch('[0-9a-f]{8}',crc) is None or
            crc!=package['transaction_crc32'].lower()):
            raise ValueError('model valid/count/generation/CRC differs from uploaded package')
    except (KeyError,TypeError,ValueError) as error:
        raise RuntimeError('A deployment identity unconfirmed: '+str(error)) from error
    return new

def exchange(port,op,data=b'',rid=1):
    port.write(request(op,data,rid));buf=b'';deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        buf+=port.read(1024);frames,buf=parse_frames(buf)
        for f in frames:
            if f['type']!=0x51 or len(f['payload'])<10:continue
            v,target,got_id,got_op,status,length=struct.unpack_from('<BBHHHH',f['payload'])
            if (v,target,got_id,got_op)!=(1,1,rid,op):continue
            if length!=len(f['payload'])-10:raise ValueError('bad response length')
            if status:raise RuntimeError(f'A rejected opcode={op:04x} status={status}')
            return f['payload'][10:]
    raise TimeoutError('result unknown; do not automatically retry model writes')

def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='mode',required=True)
    a=sub.add_parser('validate');a.add_argument('file',type=Path)
    a=sub.add_parser('wire');a.add_argument('file',type=Path);a.add_argument('--out',type=Path,required=True)
    a=sub.add_parser('capture');a.add_argument('file',type=Path)
    a=sub.add_parser('port');a.add_argument('--port',required=True)
    a.add_argument('operation',choices=['caps','status','upload','monitor']);a.add_argument('--file',type=Path)
    a.add_argument('--page',type=int,default=0);a.add_argument('--seconds',type=float,default=10)
    args=p.parse_args()
    if args.mode in ('validate','wire'):
        package=load_package(args.file)
        if args.mode=='wire':args.out.write_bytes(b''.join(request(op,data,i+1,i) for i,(op,data) in enumerate(commands(package))))
        print(json.dumps(dict(active_classes=package['active_classes'],transaction_crc32=package['transaction_crc32'],hardware_io=False)))
        return
    if args.mode=='capture':
        frames,tail=parse_frames(args.file.read_bytes())
        if tail:raise ValueError('truncated capture')
        counts={}
        for f in frames:counts[f'{f["type"]:02x}']=counts.get(f'{f["type"]:02x}',0)+1
        print(json.dumps(dict(frames=len(frames),types=counts,all_crc_valid=True,hardware_io=False)));return
    import serial
    with serial.Serial(args.port,921600,timeout=.05) as port:
        if args.operation=='caps':print(exchange(port,1).decode())
        elif args.operation=='status':print(exchange(port,2,bytes([args.page]) if args.page else b'').decode())
        elif args.operation=='upload':
            if args.file is None:raise ValueError('--file is required')
            caps=json.loads(exchange(port,1));package=load_package(args.file,caps['capacity'])
            old=json.loads(exchange(port,2,rid=2))
            status_identity(old)
            if old['loading']:raise RuntimeError('A model transaction already loading; no writes sent')
            for rid,(op,data) in enumerate(commands(package),3):exchange(port,op,data,rid)
            new=json.loads(exchange(port,2,rid=rid+1))
            verify_deployment(package,old,new)
            print(json.dumps(new))
        else:
            deadline=time.monotonic()+args.seconds;buf=b''
            while time.monotonic()<deadline:
                buf+=port.read(1024);frames,buf=parse_frames(buf)
                for f in frames:
                    if f['type']==0x41:
                        from a_gesture_tasks_capture import decode_classification
                        print(json.dumps(decode_classification(f['payload'])),flush=True)

if __name__=='__main__':main()
