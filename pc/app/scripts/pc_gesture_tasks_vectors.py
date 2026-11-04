"""PC-owned byte-exact fixtures for A/B implementers; no shared files are modified."""
from pathlib import Path
from datetime import datetime
import json
import struct
import sys
import zlib
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'EMG_Studio/EMG_Studio/emg_ml'))
from gesture_tasks_protocol import Binding,frame,map_commands,rows_bytes

def generate(out):
    vectors=[]
    for count in (2,7,14,32):
        rows=[Binding(0)]+[Binding(i,1 if i%2 else 4,True,(i-1)%9 if i%2 else 0) for i in range(1,count)]
        seq=0;blob=bytearray();commands=[]
        for op,data in map_commands(0x1234,0x10203040,rows):
            payload=struct.pack('<BBHHH',1,2,seq+1,op,len(data))+data
            wire=frame(0x50,payload,seq)
            commands.append({'opcode':f'{op:04x}','data_hex':data.hex(),'payload_hex':payload.hex(),'frame_hex':wire.hex()})
            blob.extend(wire);seq+=1
        vectors.append({'generation':0x1234,'class_count':count,'revision':0x10203040,
            'rows_hex':rows_bytes(rows).hex(),'rows_crc32':f'{zlib.crc32(rows_bytes(rows)):08x}','requests':commands})
        (out/f'map_{count}_requests.bin').write_bytes(blob)
    # Literal, implementation-independent expected row CRC used by the unit tests.
    golden={'rows_hex':'000000000000000001000101080000000200020100000000','crc32':'79b19a7a'}
    doc={'schema':1,'endianness':'little','crc32':'IEEE/zlib','static_golden':golden,'vectors':vectors,'hardware_verified':False}
    (out/'golden_vectors.json').write_text(json.dumps(doc,indent=2)+'\n',encoding='utf8')
    return doc

if __name__=='__main__':
    out=ROOT/'artifacts/pc_gesture_tasks'/datetime.now().strftime('vectors_%Y%m%d_%H%M%S_%f')
    out.mkdir(parents=True,exist_ok=False);generate(out);print(out)
