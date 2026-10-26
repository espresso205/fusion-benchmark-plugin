"""Rebuild a portable HTML report from immutable final v4 evidence."""
import argparse
import json
from pathlib import Path
from src.html_reporting import build_report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path)
    parser.add_argument('--pipeline',type=Path,help='Maintainer pipeline-status.json, retaining every requested case')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--analysis',type=Path)
    parser.add_argument('--inputs',type=Path,help='Optional ordered public input-material manifest (images, PDF, DXF or attachments)')
    parser.add_argument('--interfaces',type=Path,help='Optional private GT-bound KOR/KIR jig manifest for Interface overlay')
    parser.add_argument('--no-mesh',action='store_true',help='Generate text/metric report without CAD dependencies')
    parser.add_argument('--mesh-timeout',type=float,default=60.)
    args=parser.parse_args(argv)
    if args.input is None and args.pipeline is None:
        parser.error('at least --input or --pipeline is required')
    try:
        pipeline=json.loads(args.pipeline.read_text(encoding='utf-8')) if args.pipeline else None
        result=build_report(args.input,args.output,pipeline=pipeline,analysis=args.analysis,meshes=not args.no_mesh,timeout=args.mesh_timeout,inputs=args.inputs,interfaces=args.interfaces)
    except (ValueError,OSError) as error:
        parser.exit(2,str(error)+'\n')
    print(json.dumps(result,ensure_ascii=False))
    return 0


if __name__=='__main__': raise SystemExit(main())
