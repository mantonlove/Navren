"""Optional local query features; no output text in runtime diagnostics.

Translation never alters the original request, identities, time or source gates.
It is an additional recall view, not an evidence assertion or a stored document.
"""
import os
import re

POLICY = 'local-query-translation-feature/1'
_REASONS = {'query_translation_disabled', 'query_translation_model_missing',
            'query_translation_model_invalid', 'query_translation_dependency_missing',
            'query_translation_input_invalid', 'query_translation_input_too_long',
            'query_translation_timeout', 'query_translation_inference_failed',
            'query_translation_not_applicable', 'query_translation_input_unsupported',
            'query_translation_output_invalid', 'query_translation_numeric_mismatch',
            'query_translation_generation_limit'}


def translated_feature(query, *, applicable):
    meta={'policy':POLICY,'used':False,'reason':None,'profile_id':None}
    if not applicable:
        return None,meta
    if os.environ.get('NAVREN_QUERY_TRANSLATION_ENABLED','0')!='1':
        meta['reason']='query_translation_disabled'
        return None,meta
    try:
        from .query_translation import get_query_translator
        translator=get_query_translator()
        feature=translator.translate(query)
        if (not isinstance(feature,str) or not 1<=len(feature)<=2048
                or not re.search(r'[A-Za-z]',feature) or re.search(r'[\u3400-\u9fff]',feature)):
            raise ValueError('invalid_query_feature')
        meta.update(used=True,profile_id=translator.profile_id)
        return feature,meta
    except Exception as error:
        code=getattr(error,'code',None)
        meta['reason']=code if code in _REASONS else 'query_translation_inference_failed'
        return None,meta
