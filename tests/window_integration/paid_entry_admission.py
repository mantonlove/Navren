"""Read-only paid admission shared by standalone acceptance CLIs; never launches a model."""
from pathlib import Path
import hashlib
import importlib.util
import json

COUNTS = {'formal-business-original39':39, 'semantic-original10-three-rounds':30,
          'integration-public-original-P10-P02':2,
          'integration-finance-original-S01-S02-S03-S10':4,
          'G3-original-public12-finance10':22}

def _sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def _read(path):
    def unique(items):
        result={}
        for key,value in items:
            if key in result: raise ValueError('paid_admission_duplicate_json_key')
            result[key]=value
        return result
    return json.loads(Path(path).read_text(), object_pairs_hook=unique,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError('paid_admission_nonfinite_json')))
def _file(root, value):
    path=Path(value)
    if not path.is_absolute(): path=root/path
    if (path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root)
            or any(p.is_symlink() for p in path.parents if p.is_relative_to(root))):
        raise ValueError('paid_admission_file_not_regular_under_candidate')
    return path.resolve()

def _registry_rows(rows):
    if (not isinstance(rows,list) or len(rows)!=5 or any(not isinstance(r,dict) for r in rows) or
            {r.get('suite_id'):r.get('planned_attempts') for r in rows}!=COUNTS
            or len({r.get('suite_id') for r in rows})!=5
            or sum(r['planned_attempts'] for r in rows)!=97):
        raise ValueError('paid_admission_complete97_suite_identity_required')
    return rows

def _output_directory(root, suite_id, value):
    output=Path(value)
    if not output.is_absolute() or output.is_symlink(): raise ValueError('paid_admission_output_absolute_required')
    output=output.resolve()
    parts=root.parts
    outer=next((i for i in range(len(parts)-2) if parts[i:i+3]==('outputs','window-progress','integration')),None)
    integration=(Path(*parts[:outer+3]) if outer is not None else root/'outputs/window-progress/integration')
    allowed=(root/'outputs/window-progress/integration/formal-business-runner' if suite_id=='formal-business-original39'
             else integration/'finance-probe-runner' if suite_id=='integration-finance-original-S01-S02-S03-S10'
             else integration/'regression-cloud-runner' if suite_id=='G3-original-public12-finance10'
             else integration)
    if output==allowed or not output.is_relative_to(allowed) or any(p.is_symlink() for p in Path(value).parents):
        raise ValueError('paid_admission_output_outside_original_scope')
    return output

def admit_cli(args, *, root, suite_id, entrypoint):
    if not args.cloud: return None
    manifest_path=getattr(args,'paid_preflight_manifest',None)
    if manifest_path is None: raise ValueError('paid_preflight_manifest_required')
    root=Path(root).resolve()
    manifest_path=_file(root,manifest_path)
    manifest=_read(manifest_path)
    rows=manifest.get('suites')
    rows=_registry_rows(rows)
    if suite_id not in COUNTS or sum(r.get('suite_id')==suite_id for r in rows)!=1:
        raise ValueError('paid_admission_requested_suite_missing')
    if manifest.get('paid_budget_authorization') is None:
        raise ValueError('paid_baseline_authorization_missing')
    if manifest.get('stage')!='paid' or manifest.get('source_root')!=str(root):
        raise ValueError('paid_admission_source_or_stage_mismatch')
    source_ref=manifest['source_manifest'];source_path=_file(root,source_ref['path'])
    if _sha(source_path)!=source_ref['sha256']: raise ValueError('paid_admission_source_digest_mismatch')
    full_sources=_read(source_path)
    actual_entry=_file(root,entrypoint)
    relative=str(actual_entry.relative_to(root))
    if full_sources.get(relative)!=_sha(actual_entry): raise ValueError('paid_admission_running_entry_not_frozen')
    shared=_file(root,Path(__file__).resolve())
    if full_sources.get(str(shared.relative_to(root)))!=_sha(shared):
        raise ValueError('paid_admission_shared_gate_not_frozen')
    declared=getattr(args,'source_manifest',None)
    if declared is not None:
        runner_sources=_read(_file(root,declared))
        if not isinstance(runner_sources,dict) or any(full_sources.get(k)!=v for k,v in runner_sources.items()):
            raise ValueError('paid_admission_runner_source_not_same_full_snapshot')
    output=_output_directory(root,suite_id,args.out_dir)
    case_id=getattr(args,'public_child',None)
    requests=manifest.get('execution_requests',[])
    matches=[r for r in requests if isinstance(r,dict) and r.get('suite_id')==suite_id
        and r.get('entrypoint')==relative and r.get('output_dir')==str(output)
        and r.get('case_id')==case_id]
    if len(matches)!=1: raise ValueError('paid_admission_exact_execution_request_required')
    request=matches[0]
    if (request.get('entrypoint_sha256')!=full_sources[relative]
            or request.get('source_manifest_sha256')!=source_ref['sha256']):
        raise ValueError('paid_admission_request_source_binding_mismatch')
    if output.exists(): raise ValueError('paid_admission_fresh_output_required')
    gate_script=_file(root,'scripts/run_window_integration.py')
    if full_sources.get('scripts/run_window_integration.py')!=_sha(gate_script):
        raise ValueError('paid_admission_real_gate_program_not_frozen')
    spec=importlib.util.spec_from_file_location('_navren_standalone_paid_gate',gate_script)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    gate_out=output.with_name(output.name+'-paid-admission')
    gate_out.mkdir(parents=True,exist_ok=False)
    result=module.paid_preflight_gate(root,manifest_path,full_sources,gate_out)
    if result.get('ready') is not True: raise ValueError('paid_admission_real_preflight_rejected:'+str(result.get('reason')))
    if _sha(manifest_path)!=result.get('manifest_sha256'):
        raise ValueError('paid_admission_manifest_changed_after_gate')
    return {'suite_id':suite_id,'manifest_path':str(manifest_path),
        'manifest_sha256':result['manifest_sha256'],'source_manifest_sha256':source_ref['sha256'],
        'entrypoint':relative,'output_dir':str(output),'case_id':case_id,'gate_receipt':str(gate_out/'paid-preflight-execution.json'),
        'ready':True,'provider_calls_before_gate':0}
