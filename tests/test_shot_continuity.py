"""CPU-only continuation contract tests. Does not import ComfyUI or queue work."""
import importlib.util
import json
from pathlib import Path
import tempfile
import sys
import types
import unittest

spec=importlib.util.spec_from_file_location('continuity',Path(__file__).parents[1]/'nodes_shot_continuity.py')
C=importlib.util.module_from_spec(spec);spec.loader.exec_module(C)


class ContinuityTests(unittest.TestCase):
    def grid_case(self, sr=24000, context=0, duration=12, missing=0, extra=0, edit_duration=None):
        import torch
        from unittest.mock import patch
        frames = context + duration * 24
        target = frames + (5 - frames % 17) % 17
        ticks = round(target / 24 * 40)
        editorial = duration if edit_duration is None else edit_duration
        waveform = torch.arange(round(context / 24 * sr) + round(editorial * sr) - missing + extra,
                                dtype=torch.float32).reshape(1, 1, -1).repeat(1, 2, 1)
        audio = {'sample_rate': sr, 'waveform': waveform}
        timing = types.SimpleNamespace(
            _streams_from_latent=lambda latent: (types.SimpleNamespace(shape=(1, 4, 1, 1, 1)),
                                                types.SimpleNamespace(shape=(1, 4, 2, ticks))),
            _pixel_frames=lambda t: target,
            audio_grid_geometry=lambda vae, steps: (32000, 800, steps * 800))
        with patch.object(C.importlib, 'import_module', return_value=timing):
            result = C.fit_conditioning_grid(object(), 'latent', 'vae', audio,
                {'duration_seconds': duration, 'edit_duration_seconds': editorial}, context)
        return audio, result, target, ticks

    def test_conditioning_padding_reproduces_g04_shortfall(self):
        import torch
        before, after, target, ticks = self.grid_case()
        self.assertEqual((target, ticks), (294, 490))
        self.assertEqual(after['waveform'].shape[-1] - before['waveform'].shape[-1], 6000)
        self.assertTrue(torch.equal(before['waveform'], after['waveform'][..., :288000]))
        self.assertEqual(torch.count_nonzero(after['waveform'][..., 288000:]).item(), 0)
        self.assertEqual(before['waveform'].shape[-1], 288000)

    def test_conditioning_grid_covers_context_and_resampling_rates(self):
        import math
        import torch
        for sr in (24000, 32000, 44100, 48000):
            for context in (0, 39, 90):
                before, after, target, ticks = self.grid_case(sr=sr, context=context)
                self.assertGreaterEqual(after['waveform'].shape[-1], math.ceil(target / 24 * sr))
                self.assertGreaterEqual(after['waveform'].shape[-1], math.ceil(ticks / 40 * sr))
                self.assertTrue(torch.equal(before['waveform'], after['waveform'][..., :before['waveform'].shape[-1]]))

    def test_conditioning_padding_does_not_hide_missing_dialogue(self):
        for deficit in (1, 24000):
            with self.assertRaisesRegex(ValueError, 'missing dialogue'):
                self.grid_case(missing=deficit)

    def test_fractional_editorial_audio_pads_to_longer_model_duration(self):
        import torch
        for duration, edit, context in ((19, 18.09, 39), (18, 17.5, 39), (11, 10.43, 0)):
            before, after, target, ticks = self.grid_case(duration=duration,
                edit_duration=edit, context=context)
            length = before['waveform'].shape[-1]
            self.assertTrue(torch.equal(before['waveform'], after['waveform'][..., :length]))
            self.assertEqual(torch.count_nonzero(after['waveform'][..., length:]).item(), 0)
            with self.assertRaisesRegex(ValueError, 'missing dialogue'):
                self.grid_case(duration=duration, edit_duration=edit, context=context, missing=1)

    def test_editorial_duration_cannot_exceed_generated_shot(self):
        with self.assertRaisesRegex(ValueError, 'Editorial duration'):
            self.grid_case(duration=12, edit_duration=13)

    def test_conditioning_long_audio_is_not_trimmed_or_replaced(self):
        before, after, _, _ = self.grid_case(extra=24000)
        self.assertIs(before, after)

    def test_recorded_prepare_passes_grid_fitted_audio_to_mask(self):
        from unittest.mock import Mock, patch
        mask = Mock()
        mask.prepare.return_value = ('masked', 0, 'clip')
        original, fitted = object(), object()
        with patch.dict(sys.modules, {'nodes': types.SimpleNamespace(
                NODE_CLASS_MAPPINGS={'MiniMaxH3SongMaskedAVContext': lambda: mask})}), \
             patch.object(C, 'fit_conditioning_grid', return_value=fitted) as fit:
            result = C.CoachBatePrepareShotContinuity().prepare('latent', 'vae', 'audio_vae',
                json.dumps({'shot': {'duration_seconds': 12}}), 'run', False, original)
        self.assertEqual(result, ('masked', fitted, 0))
        self.assertIs(fit.call_args.args[3], original)
        self.assertIs(mask.prepare.call_args.args[2], fitted)

    def test_hard_anchors_use_frame_zero_and_editorial_last_frame(self):
        from PIL import Image
        from unittest.mock import Mock, patch
        hard = Mock()
        hard.apply.return_value = ('masked',)
        soft = Mock()
        mapping = {'MiniMaxH3CustomKeyframesMasked': lambda: hard,
                   'MiniMaxH3CustomKeyframes': lambda: soft}
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'anchor.png'
            Image.new('RGB', (8, 8), 'green').save(path)
            shot = {'duration_seconds': 15, 'hard_frame_anchors': True,
                    'start_image': str(path), 'end_image': str(path)}
            with patch.dict(sys.modules, {'nodes': types.SimpleNamespace(NODE_CLASS_MAPPINGS=mapping)}):
                result = C.CoachBateShotKeyframes().apply('cond', {}, 'vae', json.dumps({'shot': shot}))
        self.assertEqual(result, ('cond', 'masked'))
        self.assertEqual(json.loads(hard.apply.call_args.args[2])['positions'], [0, 359])
        self.assertEqual(hard.apply.call_args.args[3:5], ('0-based', 'center'))
        soft.apply.assert_not_called()

    def test_hard_anchor_masks_compact_without_changing_preservation(self):
        import torch
        class Nested:
            def __init__(self, parts): self.parts = parts
            def unbind(self): return self.parts
        video = torch.ones(1, 24, 3, 2, 2)
        video[:, :, 0] = 0
        audio = torch.zeros(1, 8, 2, 5)
        for packed in ((video, audio), [video, audio], Nested((video, audio))):
            latent = {'samples': 'untouched', 'noise_mask': packed}
            result = C.compact_keyframe_masks(latent)
            masks = result['noise_mask']
            masks = masks.unbind() if hasattr(masks, 'unbind') else masks
            self.assertEqual(masks[0].shape, (1, 1, 3, 2, 2))
            self.assertEqual(masks[1].shape, (1, 1, 2, 5))
            self.assertTrue(torch.equal(masks[0].expand_as(video), video))
            self.assertTrue(torch.equal(masks[1].expand_as(audio), audio))
            self.assertIs(latent['noise_mask'], packed)
            self.assertEqual(result['samples'], 'untouched')

    def test_hard_anchor_masks_reject_lossy_compaction(self):
        import torch
        video = torch.ones(1, 24, 3, 2, 2)
        audio = torch.ones(1, 8, 2, 5)
        video[:, 1, 0] = 0
        with self.assertRaisesRegex(ValueError, 'channel-dependent video'):
            C.compact_keyframe_masks({'noise_mask': (video, audio)})
        with self.assertRaisesRegex(ValueError, 'two-stream'):
            C.compact_keyframe_masks({'noise_mask': video})

    def test_soft_anchors_keep_legacy_conditioning_output(self):
        from PIL import Image
        from unittest.mock import Mock, patch
        soft = Mock()
        soft.apply.return_value = ('guided',)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'anchor.png'
            Image.new('RGB', (8, 8)).save(path)
            shot = {'duration_seconds': 15, 'start_image': str(path)}
            with patch.dict(sys.modules, {'nodes': types.SimpleNamespace(
                    NODE_CLASS_MAPPINGS={'MiniMaxH3CustomKeyframes': lambda: soft})}):
                result = C.CoachBateShotKeyframes().apply('cond', 'latent', 'vae', json.dumps({'shot': shot}))
        self.assertEqual(result, ('guided', 'latent'))

    def test_no_anchors_preserve_both_inputs(self):
        from unittest.mock import patch
        with patch.dict(sys.modules, {'nodes': types.SimpleNamespace(NODE_CLASS_MAPPINGS={})}):
            result = C.CoachBateShotKeyframes().apply('cond', 'latent', 'vae', json.dumps(
                {'shot': {'duration_seconds': 15, 'hard_frame_anchors': True}}))
        self.assertEqual(result, ('cond', 'latent'))

    def test_full_clip_audio_is_canonicalized_before_tail_crop(self):
        import numpy as np
        from unittest.mock import patch
        full = {'waveform': np.arange(252000)[None, None, :], 'sample_rate': 32000}
        module = types.SimpleNamespace(_canonical_audio=lambda audio, sr, frames: full)
        original = {'waveform': np.zeros((1, 1, 188416)), 'sample_rate': 24000}
        with patch.object(C.importlib, 'import_module', return_value=module):
            tail = C.canonical_audio_tail(object(), original, 32000, 189, 39)
        self.assertEqual(tail['waveform'].shape[-1], 52000)
        self.assertEqual(tail['waveform'][0, 0, 0], 200000)
        self.assertEqual(original['waveform'].shape[-1], 188416)

    def test_tail_does_not_bypass_full_clip_conformance_error(self):
        from unittest.mock import Mock, patch
        canonical = Mock(side_effect=ValueError('large source AV mismatch'))
        with patch.object(C.importlib, 'import_module', return_value=types.SimpleNamespace(_canonical_audio=canonical)):
            with self.assertRaisesRegex(ValueError, 'large source'):
                C.canonical_audio_tail(object(), {}, 32000, 189, 39)
        canonical.assert_called_once_with({}, 32000, 189)

    def test_short_source_context_rejected(self):
        with self.assertRaisesRegex(ValueError, 'fewer frames'):
            C.canonical_audio_tail(object(), {}, 32000, 20, 39)

    def test_prepare_loads_full_audio_but_only_context_video(self):
        import numpy as np
        from unittest.mock import Mock, patch
        video = Mock()
        video.load_video.return_value = (np.zeros((39, 1, 1, 3)), 39, 'unused cropped audio')
        full_audio = {'waveform': np.zeros((1, 1, 188416)), 'sample_rate': 24000}
        audio = Mock()
        audio.load_audio.return_value = (full_audio, 188416 / 24000)
        mask = Mock()
        mask.prepare.return_value = ('prepared', 39, 0, 39)
        canonical = Mock(return_value={'waveform': np.zeros((1, 2, 252000)), 'sample_rate': 32000})
        container = Mock()
        container.__enter__ = Mock(return_value=container)
        container.__exit__ = Mock(return_value=False)
        container.streams.video = [types.SimpleNamespace(average_rate=24, frames=189)]
        fake_nodes = types.SimpleNamespace(NODE_CLASS_MAPPINGS={
            'VHS_LoadVideoPath': lambda: video,
            'VHS_LoadAudio': lambda: audio,
            'MiniMaxH3ExistingVideoMaskedContext': lambda: mask,
        })
        with patch.dict(sys.modules, {'nodes': fake_nodes, 'av': types.SimpleNamespace(open=lambda path: container)}), \
             patch.object(C, 'read_source', return_value=('exact-receipt.mp4', 39)), \
             patch.object(C.importlib, 'import_module', return_value=types.SimpleNamespace(_canonical_audio=canonical)):
            result = C.CoachBatePrepareShotContinuity().prepare(
                'latent', 'vae', types.SimpleNamespace(audio_sample_rate=32000),
                json.dumps({'shot': {'shot_id': 'next'}}), 'run', True)
        self.assertEqual(result, ('prepared', None, 39))
        audio.load_audio.assert_called_once_with(audio_file='exact-receipt.mp4', seek_seconds=0, duration=0)
        self.assertEqual(video.load_video.call_args.kwargs['skip_first_frames'], 150)
        self.assertEqual(video.load_video.call_args.kwargs['frame_load_cap'], 39)
        canonical.assert_called_once_with(full_audio, 32000, 189)
        self.assertEqual(mask.prepare.call_args.args[4]['waveform'].shape[-1], 52000)

    def test_actual_loader_appends_outputs_without_moving_legacy_slots(self):
        directory=Path(__file__).parents[1]
        package=types.ModuleType('coachbate_continuity_test')
        package.__path__=[str(directory)]
        sys.modules[package.__name__]=package
        spec=importlib.util.spec_from_file_location(package.__name__+'.nodes',directory/'nodes.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        module._V3=False
        module._send_toast=lambda **kwargs:None
        cls=module.CoachBateShotLoader
        self.assertEqual(tuple(cls.RETURN_NAMES[31:35]),("shot_payload","video_continuation","continuation_source_shot_id","continuation_context_frames"))
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'shots.json'
            row={'shot_id':'two','duration_seconds':8,'video_prompt':'original words','video_filename_prefix':'002_two','video_continuation':True,'continuation_source_shot_id':'one'}
            path.write_text(json.dumps([row]))
            result=cls.execute(str(path),1,'fixed')['result']
            self.assertEqual(len(result),41)
            self.assertEqual(result[:4],('original words',8,'two','002_two'))
            self.assertEqual(result[32:35],(True,'one',39))
            self.assertEqual(json.loads(result[31])['shotlist_path'],str(path))
            before=cls.fingerprint_inputs('fixed',json_path=str(path))
            row['video_prompt']='latest saved words'
            row.pop('video_continuation');row.pop('continuation_source_shot_id')
            path.write_text(json.dumps([row]))
            self.assertNotEqual(before,cls.fingerprint_inputs('fixed',json_path=str(path)))
            updated=cls.execute(str(path),1,'fixed')['result']
            self.assertEqual(updated[32:35],(False,'',0))
            self.assertEqual(updated[0],'latest saved words')
            self.assertEqual(result[0],'original words')

    def test_legacy_default(self):
        self.assertEqual(C.continuation_settings({'shot_id':'one'}),(False,'',0))

    def test_strict_boolean(self):
        for value in ('false','true',0,1,None):
            with self.assertRaises(ValueError):C.continuation_settings({'video_continuation':value})

    def test_source_required(self):
        for value in ('','one'):
            with self.assertRaises(ValueError):C.continuation_settings({'shot_id':'one','video_continuation':True,'continuation_source_shot_id':value})

    def test_context_boundaries(self):
        row={'shot_id':'two','video_continuation':True,'continuation_source_shot_id':'one'}
        self.assertEqual(C.continuation_settings(row),(True,'one',39))
        for n in (5,22,56,0,39.0,True):
            with self.assertRaises(ValueError):C.continuation_settings({**row,'continuation_context_frames':n})
        for n in (39,90,141):self.assertEqual(C.continuation_settings({**row,'continuation_context_frames':n})[-1],n)

    def test_manual_guard(self):
        for row in ({'manual_input_required':True},{'video_prompt':'MARC_INPUT_REQUIRED'}):
            with self.assertRaises(ValueError):C.require_ready(row)
        C.require_ready({'video_prompt':'Ready'})

    def test_order_and_done_do_not_invalidate_source(self):
        row={'shot_id':'one','video_prompt':'same prompt','status':'REVIEW','batch_enabled':True,'episode_order':1}
        self.assertEqual(C.fingerprint(row),C.fingerprint({**row,'status':'DONE','batch_enabled':False,'episode_order':7}))
        self.assertNotEqual(C.fingerprint(row),C.fingerprint({**row,'video_prompt':'different'}))

    def test_exact_vhs_filename_and_stale_source(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);shot={'shot_id':'one','video_filename_prefix':'001_one'}
            following={'shot_id':'two','video_continuation':True,'continuation_source_shot_id':'one'}
            shotlist=root/'shots.json';shotlist.write_text(json.dumps([shot,following]))
            payload={'shot':following,'shotlist_path':str(shotlist)}
            with self.assertRaises(FileNotFoundError):C.read_source(payload,root)
            raw=root/'001_one_00029.mp4';raw.write_bytes(b'raw')
            mux=root/'001_one_00029-audio.mp4';mux.write_bytes(b'mux')
            result=C.CoachBateRecordShotOutput().record((True,[str(root/'meta.png'),str(raw),str(mux)]),json.dumps({'shot':shot}),str(root))
            self.assertEqual(result['result'][0],str(mux))
            self.assertEqual(C.read_source(payload,root),(str(mux),39))
            shot['video_prompt']='changed';shotlist.write_text(json.dumps([shot,following]))
            with self.assertLogs(C.logger, level='WARNING') as logs:
                self.assertEqual(C.read_source(payload,root),(str(mux),39))
            self.assertIn('edited after its render loaded', logs.output[0])
            for rows in ([following], [shot, shot, following]):
                shotlist.write_text(json.dumps(rows))
                with self.assertRaisesRegex(ValueError, 'exactly once'):
                    C.read_source(payload,root)
            shotlist.write_text(json.dumps([shot,following]))
            saved=C.receipt_path(root,'one')
            receipt=json.loads(saved.read_text())
            receipt['shot_id']='wrong-source'
            saved.write_text(json.dumps(receipt))
            with self.assertRaisesRegex(ValueError, 'does not belong'):
                C.read_source(payload,root)

    def test_outside_run_or_temp_output_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);file=root/'out.mp4';file.write_bytes(b'x')
            node=C.CoachBateRecordShotOutput();payload=json.dumps({'shot':{'shot_id':'one'}})
            with self.assertRaises(ValueError):node.record((False,[str(file)]),payload,str(root))
            with self.assertRaises(ValueError):node.record((True,[str(file)]),payload,str(root/'elsewhere'))

    def test_modified_output_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);file=root/'00001-audio.mp4';file.write_bytes(b'x')
            shot={'shot_id':'one'};shotlist=root/'shots.json';shotlist.write_text(json.dumps([shot]))
            C.CoachBateRecordShotOutput().record((True,[str(file)]),json.dumps({'shot':shot}),str(root))
            file.write_bytes(b'modified')
            with self.assertRaises(ValueError):C.read_source({'shot':{'shot_id':'two','video_continuation':True,'continuation_source_shot_id':'one'},'shotlist_path':str(shotlist)},root)

    def test_trim_preserves_master_alignment(self):
        import numpy as np
        node=C.CoachBateTrimShotOutput()
        payload=json.dumps({'shot':{'duration_seconds':2,'edit_duration_seconds':2}})
        images=np.arange(100)
        audio={'waveform':np.arange(100000)[None,None,:],'sample_rate':24000}
        picture,master=node.trim(images,audio,payload,39,False)
        self.assertEqual(picture.tolist(),list(range(39,87)))
        self.assertEqual(master['waveform'][0,0,0],0)
        self.assertEqual(master['waveform'].shape[-1],48000)
        _,generated=node.trim(images,audio,payload,39,True)
        self.assertEqual(generated['waveform'][0,0,0],39000)
        with self.assertRaises(ValueError):node.trim(images[:50],audio,payload,39,False)


if __name__=='__main__':unittest.main()
