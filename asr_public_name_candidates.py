"""Isolated retrieval-only prototype. Raw text is immutable; no identity selection or action."""
import ctypes,re,unicodedata

class MandarinRetrievalKey:
    """Use the already installed OS transform, not a dictionary inferred from a test answer."""
    def __init__(self):
        f=ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        p=ctypes.c_void_p;n=ctypes.c_long;u=ctypes.c_uint32;b=ctypes.c_bool
        f.CFStringCreateWithBytes.argtypes=[p,p,n,u,b];f.CFStringCreateWithBytes.restype=p
        f.CFStringCreateMutableCopy.argtypes=[p,n,p];f.CFStringCreateMutableCopy.restype=p
        f.CFStringTransform.argtypes=[p,p,p,b];f.CFStringTransform.restype=b
        f.CFStringGetLength.argtypes=[p];f.CFStringGetLength.restype=n
        f.CFStringGetMaximumSizeForEncoding.argtypes=[n,u];f.CFStringGetMaximumSizeForEncoding.restype=n
        f.CFStringGetCString.argtypes=[p,p,n,u];f.CFStringGetCString.restype=b
        f.CFRelease.argtypes=[p];f.CFRelease.restype=None
        self.f=f;self.transform=ctypes.c_void_p.in_dll(f,'kCFStringTransformMandarinLatin').value
        self.cache={}
    def __call__(self,value):
        if value in self.cache:return self.cache[value]
        data=value.encode();f=self.f;obj=f.CFStringCreateWithBytes(None,data,len(data),0x08000100,False)
        if not obj:raise ValueError('TRANSLITERATION_UNAVAILABLE')
        mutable=None
        try:
            mutable=f.CFStringCreateMutableCopy(None,0,obj)
            if not mutable or not f.CFStringTransform(mutable,None,self.transform,False):raise ValueError('TRANSLITERATION_UNAVAILABLE')
            size=f.CFStringGetMaximumSizeForEncoding(f.CFStringGetLength(mutable),0x08000100)+1
            buf=ctypes.create_string_buffer(size)
            if not f.CFStringGetCString(mutable,buf,size,0x08000100):raise ValueError('TRANSLITERATION_UNAVAILABLE')
            text=unicodedata.normalize('NFD',buf.value.decode()).casefold()
            key=re.sub('[^a-z0-9]','',text)
            self.cache[value]=key
            return key
        finally:
            if mutable:f.CFRelease(mutable)
            f.CFRelease(obj)

def distance(a,b):
    row=list(range(len(b)+1))
    for i,x in enumerate(a,1):
        out=[i]
        for j,y in enumerate(b,1):out.append(min(out[-1]+1,row[j]+1,row[j-1]+(x!=y)))
        row=out
    return row[-1]

def propose(raw,catalog,spellings,key):
    """spellings are from the actual trusted boundary's public-term registration.

    Similarity only orders suggestions. It is not a posterior or identity proof.
    No edits to raw, no A/ah mapping, no selection or financial authority.
    """
    if not isinstance(raw,str)or not 0<len(raw)<=8000:raise ValueError('INVALID_ASR_RAW')
    rows=[r for r in catalog['entities']if r['kind']=='product']
    products=[];evaluations=0;budget=2048
    for row in rows:
        names=spellings[row['public_ref']]
        # Reuse the shortest native query spelling; appended share-like spellings
        # never become share identities and are not used to interpret an uttered ah.
        term=min(names,key=lambda s:(len(s),s))
        target=key(term);best=None
        for run in re.finditer(r'[\u4e00-\u9fff]+',raw):
            for length in range(max(2,len(term)-1),len(term)+2):
                for start in range(run.start(),run.end()-length+1):
                    evaluations+=1
                    if evaluations>budget:
                        return {'raw_text':raw,'release_id':catalog['release_id'],'status':'manual_target_required',
                                'candidates':[],'selected_product_ref':None,'selected_share_ref':None,
                                'user_confirmation_required':True,'execution_authority':False,
                                'reason':'bounded_candidate_assist_exhausted; raw retained in full'}
                    end=start+length;span=raw[start:end];phonetic=key(span)
                    score=1-distance(phonetic,target)/max(len(phonetic),len(target),1)
                    literal=1-distance(span,term)/max(len(span),len(term),1)
                    item=(score,literal,-abs(len(span)-len(term)),-start,start,end,span)
                    if best is None or item>best:best=item
        if best is not None:
            products.append({'label':row['label'],'public_ref':row['public_ref'],'citation_id':row['citation_id'],
                'query_spelling':term,'raw_span':best[6],'raw_start':best[4],'raw_end':best[5],
                'phonetic_edit_similarity':best[0],'literal_edit_similarity':best[1],
                'score_is_identity_or_probability':False})
    products.sort(key=lambda r:(-r['phonetic_edit_similarity'],-r['literal_edit_similarity'],r['public_ref']))
    return {'raw_text':raw,'release_id':catalog['release_id'],'status':'needs_user_disambiguation',
            'candidates':products[:3],'selected_product_ref':None,'selected_share_ref':None,
            'user_confirmation_required':True,'execution_authority':False,
            'candidate_uses_current_public_names_only':True,'financial_values_not_changed':True,
            'share_policy':'Share IDs/codes are separate public identities; no letter inferred from audio or product name.',
            'evaluations':evaluations,'retrieval_only':True}
