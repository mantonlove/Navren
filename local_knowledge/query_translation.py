"""Pinned local Chinese-to-English QUERY FEATURES, never authoritative facts.

No downloads, sockets, text logs, query cache or persistence in this module.
Original-query identity, dates, amounts and access/evidence gates stay authoritative.
The underlying model is a generator; its output must not enter fact/graph storage.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import threading
import time

MODEL_REPOSITORY = 'Xenova/opus-mt-zh-en'
MODEL_REVISION = '39d480d52a9ea3065a1f117adfe4dbc55de10e6f'
UPSTREAM_REPOSITORY = 'Helsinki-NLP/opus-mt-zh-en'
UPSTREAM_REVISION = 'cf109095479db38d6df799875e34039d4938aaa6'
MAX_INPUT_CHARACTERS = 500
MAX_INPUT_TOKENS = 128
MAX_GENERATED_TOKENS = 96
MAX_OUTPUT_CHARACTERS = 1000
MAX_CALL_SECONDS = 2.0
MAX_RUN_SECONDS = .5
DIMENSION = 512
LAYERS = 6
ATTENTION_HEADS = 8
VOCABULARY_SIZE = 65001
SPECIAL_TOKEN_IDS = {'</s>': 0, '<unk>': 1, '<pad>': 65000}
DECODER_START_TOKEN_ID = 65000
ALGORITHM_POLICY = 'query-feature-marian-q8-source-spm-adapter1-greedy1-src128-out96-budget2s-calendar-literals-v2-month-support-v1'
NUMERIC_POLICY = 'exact remaining numeric literal multiset plus explicit calendar-month multiset; mismatch rejects feature'
CALENDAR_MONTH_NAMES = ('January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December')
CALENDAR_GUARD_POLICY = {
    'version': 'strict-calendar-month-literals/1',
    'source_pattern': r'(?<![0-9.+\-\u2212])(?P<month>1[0-2]|0?[1-9])月(?!期|龄|数|度)',
    'english_names': CALENDAR_MONTH_NAMES,
    'english_policy': 'full capitalized names with ASCII word boundaries; no abbreviations or numeric/chinese-word inference',
    'ambiguous_names': ('May', 'March'),
    'ambiguous_context': 'explicit temporal preposition/determiner, or adjacent numeric day/year; otherwise reject',
    'other_numbers': 'unchanged exact digit/decimal literal multiset; no conversion of year/amount/rate/quarter/term',
    'activation': 'only if source has an explicit Chinese calendar-month literal',
}
_CHINESE_MONTH = re.compile(CALENDAR_GUARD_POLICY['source_pattern'])
_ENGLISH_MONTH = re.compile(r'(?<![A-Za-z])(?:' + '|'.join(CALENDAR_MONTH_NAMES) + r')(?![A-Za-z])')
_LITERAL_NUMBER = re.compile(r'\d+(?:\.\d+)?')
CALENDAR_SUPPORT_POLICY = {
    'version': 'explicit-source-month-support/1',
    'source_anchors': 'N月 or finite 一 through 十二月/月份; exact month multiplicity',
    'source_duration_prefixes': ('期限', '时长', '存期', '期长', '存续期', '锁定期',
        '封闭期', '持有期', '投资期', '贷款期', '借款期', '持续', '历时', '长达'),
    'duration_modifiers': ('为', '是', '约', '达', '至少', '最多', '共', '有', '长', '：', ':'),
    'english_policy': 'full names case-insensitive; lowercase may/march without temporal context are verbs; ambiguous capitalized forms reject',
    'activation': 'every output containing an English calendar month; without one this guard adds no inference',
    'not_inferred': 'relative months, quarters, duration prefixes, amounts, currency, signs, negation or year-month relationships',
}
_CHINESE_MONTH_WORDS = ('一', '二', '三', '四', '五', '六', '七', '八', '九', '十', '十一', '十二')
_CHINESE_WORD_MONTH = re.compile(
    r'(?<![零〇一二三四五六七八九十百千万0-9.+\-\u2212])(?P<month_word>'
    + '|'.join(sorted(_CHINESE_MONTH_WORDS, key=len, reverse=True)) + r')月(?:份)?(?!期|龄|数|度)')
_ENGLISH_SUPPORT_MONTH = re.compile(
    r'(?<![A-Za-z])(?:' + '|'.join(CALENDAR_MONTH_NAMES) + r')(?![A-Za-z])', re.I)
_DURATION_MONTH_PREFIX = re.compile(
    r'(?:' + '|'.join(CALENDAR_SUPPORT_POLICY['source_duration_prefixes']) + r')'
    + r'(?:\s*(?:' + '|'.join(CALENDAR_SUPPORT_POLICY['duration_modifiers']) + r')){0,3}\s*$')


def _calendar_months_supported(original: str, output: str) -> bool:
    """Reject introduced spelled months; this does not prove translation fidelity.

    Only literal source calendar anchors support output month names. Duration
    wording and relative dates never create anchors. The numeric guard remains a
    separate contract and still decides all remaining digit literals.
    """
    target_values = []
    for match in _ENGLISH_SUPPORT_MONTH.finditer(output):
        value = match.group().casefold()
        if value in ('may', 'march'):
            before, after = output[:match.start()], output[match.end():]
            temporal = re.search(r'\b(?:in|on|during|since|until|before|after|by|from|through|for|of|this|last|next)\s*$', before, re.I)
            dated = re.match(r'\s*[0-9]{1,4}(?:st|nd|rd|th)?\b', after, re.I)
            day_before = re.search(r'\b[0-9]{1,2}\s+(?:of\s+)?$', before, re.I)
            if not (temporal or dated or day_before):
                if not match.group().islower():
                    return False
                continue
        target_values.append(tuple(name.casefold() for name in CALENDAR_MONTH_NAMES).index(value) + 1)
    if not target_values:
        return True
    source_values = []
    for pattern, group, names in ((_CHINESE_MONTH, 'month', None),
                                  (_CHINESE_WORD_MONTH, 'month_word', _CHINESE_MONTH_WORDS)):
        for match in pattern.finditer(original):
            if _DURATION_MONTH_PREFIX.search(original[:match.start()]):
                continue
            source_values.append(int(match.group(group)) if names is None else names.index(match.group(group)) + 1)
    return sorted(source_values) == sorted(target_values)


def _numeric_literals_equal(original: str, output: str) -> bool:
    """Compare literals only; this is not a semantic/currency correctness test.

    Keep the model input intact. A source N月 allows only the matching spelled
    English month, with exact multiplicity. Its numeral is removed only from the
    guard comparison. All other numbers retain the previous exact comparison.
    Without N月, an English month never stands in for an amount or other digit.
    """
    source_months = list(_CHINESE_MONTH.finditer(original))
    if not source_months:
        return sorted(_LITERAL_NUMBER.findall(original)) == sorted(_LITERAL_NUMBER.findall(output))
    target_months = list(_ENGLISH_MONTH.finditer(output))
    for match in target_months:
        if match.group() not in CALENDAR_GUARD_POLICY['ambiguous_names']:
            continue
        # Bare May/March can be modal/imperative verbs. Accept conservative
        # calendar contexts; unfamiliar wording continues as controlled refusal.
        before, after = output[:match.start()], output[match.end():]
        temporal = re.search(r'\b(?:in|on|during|since|until|before|after|by|from|through|for|of|this|last|next)\s*$', before, re.I)
        dated = re.match(r'\s*[0-9]{1,4}(?:st|nd|rd|th)?\b', after, re.I)
        day_before = re.search(r'\b[0-9]{1,2}\s+(?:of\s+)?$', before, re.I)
        if not (temporal or dated or day_before):
            return False
    source_values = sorted(int(m.group('month')) for m in source_months)
    target_values = sorted(CALENDAR_MONTH_NAMES.index(m.group()) + 1 for m in target_months)
    if source_values != target_values:
        return False
    remaining_source = _CHINESE_MONTH.sub(' ', original)
    remaining_output = _ENGLISH_MONTH.sub(' ', output)
    return sorted(_LITERAL_NUMBER.findall(remaining_source)) == sorted(_LITERAL_NUMBER.findall(remaining_output))
MODEL_ARTIFACTS = {
    'config.json': {'size': 1389, 'sha256': '293d318fce41dbf04114eac45037bb88a32d7c4ee21011a75e24a8b98ca45ad1'},
    'generation_config.json': {'size': 293, 'sha256': '8dc29fef0fe82109f94ef3c2e6ea6bded3215d357b226c34cf7b4630726766c9'},
    'tokenizer_config.json': {'size': 282, 'sha256': '08849acc0a539c4749d8665e9d6217735503a97871ccebeea8a762d5fba1acf7'},
    'special_tokens_map.json': {'size': 74, 'sha256': '5e4d1f5e759d74cb1c2fe1d165cfc62b5237aa904de759380cd6f43042eec723'},
    'tokenizer.json': {'size': 6381339, 'sha256': 'b306d0301cf280bfd647d7067b5ade2a97b987e6d678df110703c002433643ff'},
    'source.spm': {'size': 804677, 'sha256': 'e27a3a1b539f4959ec72ea60e453f49156289f95d4e6000b29332efc45616203'},
    'onnx/encoder_model_quantized.onnx': {'size': 52899742, 'sha256': '84d5e171b626bc8b6b220d022ac58696e9528c25deeacca62b5cbf4364547a99'},
    'onnx/decoder_model_merged_quantized.onnx': {'size': 60212804, 'sha256': 'c6b7f04ff1ba0fbd1bf6852599b4c0cad6fe512d57cd887f44ef36cf705424cb'},
}
ERROR_CODES = frozenset({'query_translation_model_missing', 'query_translation_model_invalid',
    'query_translation_dependency_missing', 'query_translation_input_invalid',
    'query_translation_not_applicable', 'query_translation_input_too_long',
    'query_translation_input_unsupported', 'query_translation_timeout',
    'query_translation_inference_failed', 'query_translation_generation_limit',
    'query_translation_output_invalid', 'query_translation_numeric_mismatch'})

class TranslationError(RuntimeError):
    def __init__(self, code):
        self.code = code if code in ERROR_CODES else 'query_translation_inference_failed'
        super().__init__(self.code)

def default_model_directory() -> Path:
    configured = os.environ.get('NAVREN_QUERY_TRANSLATION_DIR', '').strip()
    return Path(configured).expanduser() if configured else (Path.home() / 'Library' / 'Application Support' /
        'Navren' / 'models' / 'opus-mt-zh-en-onnx-q8')

def _load_verified_artifacts(directory: Path) -> dict[str, bytes]:
    try:
        path = directory / 'manifest.json'
        if not path.exists(): raise TranslationError('query_translation_model_missing')
        if path.is_symlink() or path.stat().st_size > 16384: raise TranslationError('query_translation_model_invalid')
        manifest = json.loads(path.read_bytes())
        if (not isinstance(manifest, dict) or manifest.get('format_version') != 1
                or manifest.get('repository') != MODEL_REPOSITORY or manifest.get('revision') != MODEL_REVISION
                or manifest.get('algorithm_policy') != ALGORITHM_POLICY or manifest.get('complete') is not True
                or manifest.get('profile_id') != translation_profile_metadata()['profile_id']
                or not isinstance(manifest.get('artifacts'), dict) or set(manifest['artifacts']) != set(MODEL_ARTIFACTS)):
            raise TranslationError('query_translation_model_invalid')
        artifacts = {}
        for name, expected in MODEL_ARTIFACTS.items():
            file = directory / name; record = manifest['artifacts'][name]
            if (not isinstance(record, dict) or record.get('sha256') != expected['sha256']
                    or record.get('size') != expected['size'] or file.is_symlink()
                    or file.stat().st_size != expected['size']):
                raise TranslationError('query_translation_model_invalid')
            with file.open('rb') as stream: data = stream.read(expected['size'] + 1)
            if len(data) != expected['size'] or hashlib.sha256(data).hexdigest() != expected['sha256']:
                raise TranslationError('query_translation_model_invalid')
            artifacts[name] = data
        config = json.loads(artifacts['config.json'])
        generation = json.loads(artifacts['generation_config.json'])
        tokenizer = json.loads(artifacts['tokenizer_config.json'])
        if (config.get('model_type') != 'marian' or config.get('d_model') != DIMENSION
                or config.get('encoder_layers') != LAYERS or config.get('decoder_layers') != LAYERS
                or config.get('decoder_attention_heads') != ATTENTION_HEADS
                or config.get('vocab_size') != VOCABULARY_SIZE or not config.get('is_encoder_decoder')
                or config.get('max_position_embeddings') != 512
                or any(config.get(k) != v or generation.get(k) != v for k, v in
                       (('eos_token_id', 0), ('pad_token_id', 65000), ('decoder_start_token_id', 65000)))
                or generation.get('bad_words_ids') != [[65000]]
                or generation.get('num_beams') != 6 or tokenizer.get('source_lang') != 'zho'
                or tokenizer.get('target_lang') != 'eng'):
            raise TranslationError('query_translation_model_invalid')
        return artifacts
    except TranslationError: raise
    except FileNotFoundError: raise TranslationError('query_translation_model_missing') from None
    except Exception: raise TranslationError('query_translation_model_invalid') from None


def _proto_fields(data):
    """Read bounded, hash-pinned SentencePiece ModelProto fields, no protobuf runtime."""
    def varint(offset):
        value = shift = 0
        while offset < len(data) and shift <= 63:
            byte = data[offset]; offset += 1
            value |= (byte & 127) << shift
            if byte < 128: return value, offset
            shift += 7
        raise TranslationError('query_translation_model_invalid')
    offset = 0
    while offset < len(data):
        key, offset = varint(offset); field, wire = key >> 3, key & 7
        if not field: raise TranslationError('query_translation_model_invalid')
        if wire == 0: value, offset = varint(offset)
        else:
            if wire == 2: size, offset = varint(offset)
            elif wire == 5: size = 4
            elif wire == 1: size = 8
            else: raise TranslationError('query_translation_model_invalid')
            if offset + size > len(data): raise TranslationError('query_translation_model_invalid')
            value = data[offset:offset + size]; offset += size
        yield field, wire, value


def _source_tokenizer(artifacts):
    """Restore actual source SPM instead of the broken merged export tokenizer.

    The pinned JSON has a null precompiled charmap and merges source/target
    vocabularies; its Unigram unk_id=2 is the comma in the joint model vocabulary.
    Source SentencePiece scores/charmap come from source.spm; piece strings map
    into the pinned joint Marian vocabulary. Target decode uses that joint map.
    """
    from tokenizers import Tokenizer, models, normalizers, pre_tokenizers, decoders
    shared = json.loads(artifacts['tokenizer.json'])
    vocab = shared['model']['vocab']
    joint = {piece: index for index, (piece, _) in enumerate(vocab)}
    if len(vocab) != VOCABULARY_SIZE or len(joint) != VOCABULARY_SIZE or any(
            joint.get(token) != value for token, value in SPECIAL_TOKEN_IDS.items()):
        raise TranslationError('query_translation_model_invalid')
    messages = list(_proto_fields(artifacts['source.spm']))
    pieces = []
    for field, wire, value in messages:
        if field != 1: continue
        properties = {n: v for n, _, v in _proto_fields(value)}
        piece = properties[1].decode('utf-8')
        score = struct.unpack('<f', properties[2])[0]
        pieces.append((piece, score))
    normalizer = {n: v for n, _, v in _proto_fields(next(v for n, _, v in messages if n == 3))}
    if (len(pieces) != 32000 or pieces[0][0] != '<unk>'
            or normalizer.get(1) != b'nmt_nfkc' or len(normalizer.get(2, b'')) != 237538
            or normalizer.get(3, 1) != 1 or normalizer.get(4, 1) != 1
            or normalizer.get(5, 1) != 1):
        raise TranslationError('query_translation_model_invalid')
    tokenizer = Tokenizer(models.Unigram(pieces, unk_id=0))
    tokenizer.normalizer = normalizers.Precompiled(normalizer[2])
    tokenizer.pre_tokenizer = pre_tokenizers.Sequence([
        pre_tokenizers.WhitespaceSplit(), pre_tokenizers.Metaspace(replacement='▁', prepend_scheme='always')])
    decoder = decoders.Metaspace(replacement='▁', prepend_scheme='always')
    return tokenizer, decoder, joint, [piece for piece, _ in vocab]


def translation_profile_metadata() -> dict:
    """Pure fixed-policy description: no model load, file read or query handling."""
    fingerprint = {'repository': MODEL_REPOSITORY, 'revision': MODEL_REVISION,
        'artifacts': MODEL_ARTIFACTS, 'policy': ALGORITHM_POLICY,
        'max_input_characters': MAX_INPUT_CHARACTERS, 'max_input_tokens': MAX_INPUT_TOKENS,
        'max_generated_tokens': MAX_GENERATED_TOKENS, 'max_output_characters': MAX_OUTPUT_CHARACTERS,
        'max_call_seconds': MAX_CALL_SECONDS, 'max_run_seconds': MAX_RUN_SECONDS,
        'numeric_policy': NUMERIC_POLICY, 'calendar_literal_guard': CALENDAR_GUARD_POLICY,
        'calendar_support_guard': CALENDAR_SUPPORT_POLICY}
    digest = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'profile_id': 'opus-mt-zh-en-query-feature-q8:' + digest,
        'repository': MODEL_REPOSITORY, 'revision': MODEL_REVISION,
        'upstream_repository': UPSTREAM_REPOSITORY, 'upstream_revision': UPSTREAM_REVISION,
        'artifacts': {name: dict(item) for name, item in MODEL_ARTIFACTS.items()},
        'algorithm_policy': ALGORITHM_POLICY, 'decoding': 'greedy argmax', 'num_beams': 1,
        'source_language': 'zh', 'target_language': 'en',
        'max_input_characters': MAX_INPUT_CHARACTERS, 'max_input_tokens': MAX_INPUT_TOKENS,
        'max_generated_tokens': MAX_GENERATED_TOKENS, 'max_output_characters': MAX_OUTPUT_CHARACTERS,
        'max_call_seconds': MAX_CALL_SECONDS, 'max_run_seconds': MAX_RUN_SECONDS,
        'special_token_ids': dict(SPECIAL_TOKEN_IDS), 'decoder_start_token_id': DECODER_START_TOKEN_ID,
        'numeric_policy': NUMERIC_POLICY,
        'calendar_literal_guard': dict(CALENDAR_GUARD_POLICY),
        'calendar_support_guard': dict(CALENDAR_SUPPORT_POLICY),
        'tokenizer_adapter': 'hash-pinned source.spm charmap/source32000 scores to joint65001 IDs; reject absent piece',
        'query_only': True, 'authority': False, 'query_network_requests': 0}


class QueryTranslator:
    decoding = 'greedy argmax, num_beams=1'
    model_repository = MODEL_REPOSITORY
    model_revision = MODEL_REVISION

    def __init__(self, directory: Path):
        artifacts = _load_verified_artifacts(directory)
        try:
            import numpy as np
            import onnxruntime as ort
            import tokenizers
        except Exception:
            raise TranslationError('query_translation_dependency_missing') from None
        self._np = np; self._ort = ort; self._lock = threading.Lock()
        self.profile_id = translation_profile_metadata()['profile_id']
        try:
            self._tokenizer, self._decoder, self._joint, self._pieces = _source_tokenizer(artifacts)
            ort.disable_telemetry_events()
            options = ort.SessionOptions(); options.intra_op_num_threads = 2; options.inter_op_num_threads = 1
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            options.log_severity_level = 4; options.enable_profiling = False
            self._encoder = ort.InferenceSession(artifacts['onnx/encoder_model_quantized.onnx'],
                                                 sess_options=options, providers=['CPUExecutionProvider'])
            self._merged = ort.InferenceSession(artifacts['onnx/decoder_model_merged_quantized.onnx'],
                                                sess_options=options, providers=['CPUExecutionProvider'])
            self._encoder_inputs = {item.name for item in self._encoder.get_inputs()}
            self._encoder_output = self._encoder.get_outputs()[0].name
            self._cache_names = {f'past_key_values.{layer}.{side}.{kind}'
                for layer in range(LAYERS) for side in ('encoder', 'decoder') for kind in ('key', 'value')}
            self._decoder_inputs = {item.name for item in self._merged.get_inputs()}
            self._decoder_outputs = [item.name for item in self._merged.get_outputs()]
            expected_outputs = {'logits'} | {name.replace('past_key_values.', 'present.') for name in self._cache_names}
            if (self._encoder_inputs != {'input_ids', 'attention_mask'}
                    or self._decoder_inputs != self._cache_names | {'input_ids', 'encoder_attention_mask', 'encoder_hidden_states', 'use_cache_branch'}
                    or set(self._decoder_outputs) != expected_outputs
                    or self._encoder.get_providers() != ['CPUExecutionProvider']
                    or self._merged.get_providers() != ['CPUExecutionProvider']):
                raise TranslationError('query_translation_model_invalid')
        except TranslationError: raise
        except Exception: raise TranslationError('query_translation_model_invalid') from None

    def _run(self, session, output_names, feeds, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0: raise TranslationError('query_translation_timeout')
        options = self._ort.RunOptions()
        timer = threading.Timer(min(remaining, MAX_RUN_SECONDS), lambda: setattr(options, 'terminate', True))
        timer.daemon = True; timer.start()
        try:
            output = session.run(output_names, feeds, run_options=options)
            if options.terminate or time.monotonic() >= deadline:
                raise TranslationError('query_translation_timeout')
            return output
        except TranslationError: raise
        except Exception:
            raise TranslationError('query_translation_timeout' if options.terminate or time.monotonic() >= deadline
                                   else 'query_translation_inference_failed') from None
        finally: timer.cancel()

    def _input_ids(self, query):
        if not isinstance(query, str) or not query.strip(): raise TranslationError('query_translation_input_invalid')
        if len(query) > MAX_INPUT_CHARACTERS: raise TranslationError('query_translation_input_too_long')
        if not re.search(r'[\u3400-\u9fff]', query): raise TranslationError('query_translation_not_applicable')
        if any(token in query for token in ('</s>', '<s>', '<unk>', '<pad>')):
            raise TranslationError('query_translation_input_unsupported')
        encoded = self._tokenizer.encode(query, add_special_tokens=False)
        if not encoded.ids or len(encoded.ids) + 1 > MAX_INPUT_TOKENS:
            raise TranslationError('query_translation_input_too_long')
        # Unknown source pieces are rejected, never accepted through the export's
        # erroneous unk_id=2/comma. Keep control tokens out of the source body.
        if any(piece not in self._joint or piece in ('<unk>', '<s>', '</s>', '<pad>') for piece in encoded.tokens):
            raise TranslationError('query_translation_input_unsupported')
        return [self._joint[piece] for piece in encoded.tokens] + [SPECIAL_TOKEN_IDS['</s>']]

    def translate_query(self, query: str) -> str:
        """Return an ephemeral retrieval feature; never replace original gates/query.

        Greedy decoding differs from upstream num_beams=6. Non-EOS truncation,
        changed/introduced numeric or calendar literals, OOV pieces and timeouts reject the
        feature. Callers retain normal original-query retrieval on rejection.
        """
        deadline = time.monotonic() + MAX_CALL_SECONDS
        try:
            source = self._input_ids(query)
        except TranslationError: raise
        except Exception: raise TranslationError('query_translation_inference_failed') from None
        if not self._lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise TranslationError('query_translation_timeout')
        try:
            return self._translate(query, source, deadline)
        except TranslationError: raise
        except Exception: raise TranslationError('query_translation_inference_failed') from None
        finally: self._lock.release()

    def translate(self, query: str) -> str:
        """Public interface: ephemeral English feature, with stable-code failures."""
        return self.translate_query(query)

    def _translate(self, original_query, source, deadline):
        np = self._np
        input_ids = np.asarray([source], dtype=np.int64)
        attention = np.ones_like(input_ids)
        hidden = self._run(self._encoder, [self._encoder_output],
                           {'input_ids': input_ids, 'attention_mask': attention}, deadline)[0]
        if hidden.shape != (1, len(source), DIMENSION) or not np.isfinite(hidden).all():
            raise TranslationError('query_translation_inference_failed')
        cache = {name: np.zeros((1, ATTENTION_HEADS, 0, DIMENSION // ATTENTION_HEADS), dtype=np.float32)
                 for name in self._cache_names}
        generated = []; token = DECODER_START_TOKEN_ID
        for step in range(MAX_GENERATED_TOKENS):
            feeds = {'input_ids': np.asarray([[token]], dtype=np.int64),
                     'encoder_hidden_states': hidden, 'encoder_attention_mask': attention,
                     'use_cache_branch': np.asarray([step > 0], dtype=np.bool_), **cache}
            outputs = dict(zip(self._decoder_outputs, self._run(self._merged, self._decoder_outputs, feeds, deadline)))
            logits = outputs['logits']
            if logits.shape != (1, 1, VOCABULARY_SIZE) or not np.isfinite(logits).all():
                raise TranslationError('query_translation_inference_failed')
            scores = logits[0, -1].copy(); scores[SPECIAL_TOKEN_IDS['<pad>']] = -np.inf
            token = int(np.argmax(scores))
            if token == SPECIAL_TOKEN_IDS['</s>']:
                if not generated: raise TranslationError('query_translation_output_invalid')
                output = self._decoder.decode([self._pieces[value] for value in generated]).strip()
                if not output or len(output) > MAX_OUTPUT_CHARACTERS or not re.search('[A-Za-z]', output):
                    raise TranslationError('query_translation_output_invalid')
                if (not _numeric_literals_equal(original_query, output)
                        or not _calendar_months_supported(original_query, output)):
                    raise TranslationError('query_translation_numeric_mismatch')
                return output
            if token == SPECIAL_TOKEN_IDS['<unk>']:
                raise TranslationError('query_translation_output_invalid')
            generated.append(token)
            for name in self._cache_names:
                # Optimum merged cache branch outputs empty cross-attention PKV;
                # preserve its actual first-step encoder cache on later steps.
                if step and '.encoder.' in name: continue
                value = outputs[name.replace('past_key_values.', 'present.')]
                length = len(source) if '.encoder.' in name else step + 1
                if value.shape != (1, ATTENTION_HEADS, length, DIMENSION // ATTENTION_HEADS) or not np.isfinite(value).all():
                    raise TranslationError('query_translation_inference_failed')
                cache[name] = value
        raise TranslationError('query_translation_generation_limit')


_lock = threading.Lock()
_translator = None
_directory = None
def get_query_translator() -> QueryTranslator:
    """Lazy shared local sessions; this getter never downloads or stores queries."""
    global _translator, _directory
    directory = default_model_directory(); key = str(directory.absolute())
    with _lock:
        if _translator is None or _directory != key:
            _translator = QueryTranslator(directory); _directory = key
        return _translator
