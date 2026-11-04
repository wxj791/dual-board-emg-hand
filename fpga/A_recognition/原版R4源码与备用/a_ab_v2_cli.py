"""Offline model packaging and explicit A-v2 PC commands (921600/8N1).

Only the `port` subcommand opens a serial port. Build/test/package never do.
"""
import argparse
import binascii
import hashlib
import json
from pathlib import Path
import struct
import time
import zlib

META_TEMPLATE='{{"format":"EMG-A-AB-V2-1","classes":{count:5d},"architecture":"clean-cnn256-stat304-q23-q8","stride":32}}'

def metadata(count):
    assert 2<=count<=65534
    return META_TEMPLATE.format(count=count).encode('ascii')

def frame(kind,payload,seq=0,timestamp=0):
    body=struct.pack('<BBHHI',1,kind,seq,len(payload),timestamp)+payload
    assert len(payload)<=1024
    return b'\xa5\x5a'+body+struct.pack('<H',binascii.crc_hqx(body,0xffff))

def request(op,data=b'',rid=1,seq=0):
    return frame(0x50,struct.pack('<BBHHH',1,1,rid,op,len(data))+data,seq)

def parse_frames(data):
    result=[];pos=0
    while pos+14<=len(data):
        if data[pos:pos+2]!=b'\xa5\x5a': pos+=1;continue
        ver,kind,seq,size,stamp=struct.unpack_from('<BBHHI',data,pos+2)
        if ver!=1 or size>1024: pos+=1;continue
        end=pos+14+size
        if end>len(data): break
        assert binascii.crc_hqx(data[pos+2:end-2],0xffff)==int.from_bytes(data[end-2:end],'little'),'CRC'
        result.append(dict(type=kind,seq=seq,timestamp=stamp,payload=data[pos+12:end-2]));pos=end
    return result,data[pos:]

def transaction_crc(meta,banks):
    crc=zlib.crc32(meta)
    for b,values in enumerate(banks):
        for a,v in enumerate(values): crc=zlib.crc32(struct.pack('<BII',b,a,int(v)&0xffffffff),crc)
    return crc

def banks_from_legacy(path):
    raw=Path(path).read_bytes();banks=[[] for _ in range(8)]
    for pos in range(12,len(raw)-12,12):
        packet=raw[pos:pos+12];assert packet[:3]==b'\xc3\x3c\xf4'
        b,a,v=struct.unpack('<BHI',packet[3:10]);assert a==len(banks[b])
        banks[b].append(v)
    return banks

def validate(banks,count,capacity):
    assert 2<=count<=capacity
    assert list(map(len,banks))==[304*count,304,2*count,1280,160,32*count,2*count,count]
    for b,values in enumerate(banks):
        assert all(0<=int(v)<=0xffffffff for v in values)
    assert all(v<=65535 for b in [0,1,7] for v in banks[b])
    assert all(v<=255 for v in banks[5])
    assert all(v<=31 for v in banks[4][1:144:2]+banks[4][144:152]+banks[6][1::2])
    assert banks[4][152]==15
    # Exact worst-case arithmetic bounds, independent of the observed recording.
    def signed(v,bits): return v-(1<<bits) if v&(1<<(bits-1)) else v
    for c in range(count):
        linear_bias=signed(banks[2][c*2]|(banks[2][c*2+1]<<32),48)
        bound=abs(linear_bias)+sum(abs(signed(w,16))*65535 for w in banks[0][c*304:(c+1)*304])
        fc_bound=abs(signed(banks[6][c*2],32))+sum(abs(signed(w,8))*127 for w in banks[5][c*32:(c+1)*32])
        shift=banks[6][c*2+1]
        fc_bound=(fc_bound+(0 if shift==0 else 1<<(shift-1)))>>shift
        assert fc_bound<1<<23 and bound+(fc_bound<<15)<1<<47

def write_package(path,banks,count,capacity,names=None):
    validate(banks,count,capacity);meta=metadata(count)
    doc=dict(format='EMG-A-AB-V2-1',active_classes=count,capacity_checked=capacity,
             metadata_ascii=meta.decode(),class_names=names,banks=banks,
             transaction_crc32=f'{transaction_crc(meta,banks):08x}',flash_persistent=False)
    Path(path).write_text(json.dumps(doc,ensure_ascii=False,indent=2),encoding='utf8')
    return doc

def package_trained(linear_dir,cnn_dir,path,capacity):
    import numpy as np
    from a_fusion_memory import load_cnn
    from pc_ml.fixed_linear import FixedLinear
    linear,info=FixedLinear.load(Path(linear_dir));cnn,wave_shifts,cnn_info=load_cnn(Path(cnn_dir))
    k=len(linear.classes_)
    assert np.array_equal(linear.classes_,np.arange(k)),'class IDs must be continuous from rest=0'
    # sklearn stores one hyperplane for a binary classifier. Expand it to the
    # two explicit scores used by the FPGA and by the model package contract.
    if k==2 and linear.weights_.shape==(1,304):
        linear.weights_=np.concatenate([-linear.weights_.astype(np.int64),linear.weights_.astype(np.int64)])
        linear.bias_=np.concatenate([-linear.bias_.astype(np.int64),linear.bias_.astype(np.int64)])
    assert linear.weights_.shape==(k,304) and linear.fraction_==23
    assert np.all((linear.weights_>=-32768)&(linear.weights_<=32767))
    assert cnn.layers['fc']['weights'].shape==(k,32)
    assert cnn.layers['fc']['output_fraction']==8
    assert info['metadata']['class_names']==cnn_info['metadata']['class_names'],'linear/CNN class ordering differs'
    assert len(info['metadata']['class_names'])==k
    assert info['metadata'].get('window',256)==256
    words=np.zeros((160,8),dtype=np.uint32);pos=0;bias=[];shifts=[]
    for name,shape in [('stem',(16,8,5)),('conv2',(24,16,3)),('conv3',(32,24,3))]:
        layer=cnn.layers[name];assert layer['weights'].shape==shape
        assert layer.get('groups',1)==1 and layer.get('dilation',1)==1
        w=layer['weights'].reshape(shape[0],-1)
        for term in range(w.shape[1]):
            for oc in range(shape[0]): words[pos,oc//4]|=np.uint32((int(w[oc,term])&255)<<((oc%4)*8))
            pos+=1
        bias.extend(layer['bias'].tolist());shifts.extend(layer['shifts'].tolist())
    cfg=[0]*160
    for i,(b,s) in enumerate(zip(bias,shifts)):cfg[2*i]=int(b)&0xffffffff;cfg[2*i+1]=int(s)
    cfg[144:152]=list(map(int,wave_shifts));cfg[152]=15;cfg[153]=2097152
    def split48(v): return [int(v)&0xffffffff,(int(v)>>32)&65535]
    fc=cnn.layers['fc']
    actions=[int(v['output']['id']) for v in info['metadata']['gesture_profile']['classes']]
    banks=[(linear.weights_.reshape(-1).astype(np.int64)&65535).tolist(),
           (linear.offset_.astype(np.int64)&65535).tolist(),sum([split48(v) for v in linear.bias_],[]),
           [int(v) for v in words.reshape(-1)],cfg,
           (fc['weights'].reshape(-1).astype(np.int64)&255).tolist(),
           sum([[int(b)&0xffffffff,int(s)] for b,s in zip(fc['bias'],fc['shifts'])],[]),actions]
    return write_package(path,banks,k,capacity,info['metadata']['class_names'])

def commands(package,chunk=128):
    count=package['active_classes'];banks=package['banks'];validate(banks,count,package['capacity_checked'])
    meta=metadata(count);yield 0x102,struct.pack('<I',len(meta))+meta
    for b,values in enumerate(banks):
        for a in range(0,len(values),chunk):
            batch=values[a:a+chunk]
            yield 0x103,struct.pack('<BIH',b,a,len(batch))+struct.pack('<'+'I'*len(batch),*batch)
    yield 0x104,struct.pack('<I',transaction_crc(meta,banks))

def exchange(port,op,data=b'',rid=1):
    port.write(request(op,data,rid));buf=b'';deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        buf+=port.read(1024);frames,buf=parse_frames(buf)
        for f in frames:
            if f['type']!=0x51:continue
            ver,target,got_id,got_op,status,length=struct.unpack_from('<BBHHHH',f['payload'])
            assert (ver,target,got_id,got_op)==(1,1,rid,op)
            payload=f['payload'][10:];assert len(payload)==length
            if status: raise RuntimeError(f'opcode={op:04x} status={status}')
            return payload
    raise TimeoutError('A v2 did not reply; do not blindly retry replay/model writes')

def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    pkg=sub.add_parser('package');pkg.add_argument('--linear',type=Path);pkg.add_argument('--cnn',type=Path)
    pkg.add_argument('--legacy-f4',type=Path);pkg.add_argument('--capacity',type=int,required=True);pkg.add_argument('--out',type=Path,required=True)
    wire=sub.add_parser('wire');wire.add_argument('package',type=Path);wire.add_argument('--out',type=Path,required=True)
    io=sub.add_parser('port');io.add_argument('--port',required=True);io.add_argument('operation',choices=['caps','status','source','telemetry','upload','replay','abort'])
    io.add_argument('--value',type=int);io.add_argument('--file',type=Path)
    a=p.parse_args()
    if a.command=='package':
        if a.legacy_f4:
            banks=banks_from_legacy(a.legacy_f4);write_package(a.out,banks,len(banks[7]),a.capacity)
        else:package_trained(a.linear,a.cnn,a.out,a.capacity)
        print(a.out);return
    if a.command=='wire':
        pkg=json.loads(a.package.read_text(encoding='utf8'))
        a.out.write_bytes(b''.join(request(op,data,i+1,i) for i,(op,data) in enumerate(commands(pkg))))
        print(a.out);return
    import serial
    with serial.Serial(a.port,921600,timeout=.05) as port:
        if a.operation=='upload':
            pkg=json.loads(a.file.read_text(encoding='utf8'));caps=json.loads(exchange(port,1))
            validate(pkg['banks'],pkg['active_classes'],caps['capacity'])
            for rid,(op,data) in enumerate(commands(pkg),1):exchange(port,op,data,rid)
        elif a.operation=='replay':
            # CSV columns must be wireless wire order; no automatic inference of channel order.
            import numpy as np
            data=np.loadtxt(a.file,delimiter=',',dtype=np.float32);assert data.ndim==2 and data.shape[1]==8 and np.isfinite(data).all()
            exchange(port,0x100,b'\x01')
            for rid,start in enumerate(range(0,len(data),31),1):
                batch=data[start:start+31];exchange(port,0x108,struct.pack('<H',len(batch))+batch.astype('<f4').tobytes(),rid)
        else:
            op={'caps':1,'status':2,'source':0x100,'telemetry':0x101,'abort':0x105}[a.operation]
            data=struct.pack('<B',a.value) if op==2 and a.value is not None else struct.pack('<B',a.value) if op==0x100 else struct.pack('<H',a.value) if op==0x101 else b''
            print(exchange(port,op,data).decode('utf8'))

if __name__=='__main__':main()
