"""Generate an analytic, non-private STEP example and a frozen CLI manifest."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output',type=Path,required=True,help='new directory; never overwritten')
    args = ap.parse_args()
    folder = args.output.resolve()
    folder.mkdir(parents=True,exist_ok=False)
    step = folder/'box.step'
    writer = STEPControl_Writer()
    writer.Transfer(BRepPrimAPI_MakeBox(10,20,30).Shape(),STEPControl_AsIs)
    writer.Write(str(step))
    digest = hashlib.sha256(step.read_bytes()).hexdigest()
    annotations = folder/'annotations.json'
    annotations.write_text(json.dumps({'gt_sha256':digest,
        'items':[{'id':'width','type':'plane_pair','axis':'x','nominal':10,'tol':.01}],
        'sections':[{'id':'middle','axis':'z','offset':15}]},indent=2)+'\n',encoding='utf-8')
    row = {'case':'box','gt':str(step),'candidate':str(step),'annotations':str(annotations),
           'expected_hashes':{'gt':digest,'candidate':digest,
                              'annotations':hashlib.sha256(annotations.read_bytes()).hexdigest()}}
    (folder/'cases.json').write_text(json.dumps({'cases':[row]},indent=2)+'\n',encoding='utf-8')
    print(folder/'cases.json')


if __name__=='__main__':
    main()
