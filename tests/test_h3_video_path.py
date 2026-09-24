"""CPU-only dynamic reference routing, with no render or model loading."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('video_paths', Path(__file__).parents[1] / 'nodes_h3_video_path.py')
V = importlib.util.module_from_spec(spec)
spec.loader.exec_module(V)


class VideoReferencesTests(unittest.TestCase):
    def test_empty_slots_are_independent_and_default_audio_is_off(self):
        values = V.resolve_video_references({})
        self.assertEqual(values[:3], ('', '', ''))
        self.assertTrue(all(json.loads(v)['include_audio'] is False for v in values[3:]))
        with patch.dict(sys.modules, {'nodes': None}):
            self.assertEqual(V.CoachBateLoadH3VideoPath().load_video_path(''), (None, None, 0))

    def test_paths_ranges_and_aliases(self):
        values = V.resolve_video_references({'reference_video_1': ' clip-a.mp4 ',
            'reference_video_1_settings': {'skip_first_frames': 222, 'frame_load_cap': 107},
            'h3_reference_videos': ['ignored.mp4', 'clip-b.mp4', 'clip-c.mp4'],
            'h3_ref_video_3': ''})
        self.assertEqual(values[:3], ('clip-a.mp4', 'clip-b.mp4', ''))
        self.assertEqual(json.loads(values[3])['skip_first_frames'], 222)
        self.assertEqual(json.loads(values[4])['skip_first_frames'], 0)

    def test_invalid_settings_fail_clearly(self):
        for value in ({'frame_load_cap': -1}, {'skip_first_frames': 2.5},
                      {'select_every_nth': 0}, {'include_audio': 'false'},
                      {'force_rate': float('nan')}, {'force_rate': True},
                      {'custom_width': True}, {'typo': 1}, {'format': ''}, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                V.video_settings(value)
        for row in ({'reference_video_1': {}}, {'h3_reference_videos': ['a'] * 4},
                    {'h3_reference_videos': 'not-array'}):
            with self.assertRaises(ValueError): V.resolve_video_references(row)

    def test_vhs_gets_exact_path_range_and_optional_audio(self):
        loader = Mock()
        frames, audio = object(), object()
        loader.load_video.return_value = (frames, 107, audio, {})
        factory = Mock(return_value=loader)
        factory.INPUT_TYPES.return_value = {'optional': {'format': (['None', 'AnimateDiff'],)}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'exact_00029-audio.mp4'
            path.write_bytes(b'fake-video')
            with patch.dict(sys.modules, {'nodes': types.SimpleNamespace(NODE_CLASS_MAPPINGS={'VHS_LoadVideoPath': factory})}):
                node = V.CoachBateLoadH3VideoPath()
                options = {'force_rate': 24, 'skip_first_frames': 361, 'frame_load_cap': 107, 'custom_width': 640}
                self.assertEqual(node.load_video_path(str(path), json.dumps(options)), (frames, None, 107))
                loader.load_video.assert_called_once_with(video=str(path), **{k: v for k, v in
                    V.video_settings(options).items() if k != 'include_audio'})
                loader.load_video.return_value = {'result': (frames, 107, audio, {})}
                self.assertEqual(node.load_video_path(str(path), '{"include_audio":true}'), (frames, audio, 107))
                with self.assertRaisesRegex(ValueError, 'Unknown installed'):
                    node.load_video_path(str(path), '{"format":"missing"}')

    def test_missing_file_never_falls_back_to_another_video(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'not found'):
                V.CoachBateLoadH3VideoPath().load_video_path(str(Path(directory) / 'missing.mp4'))

    def test_source_file_edits_invalidate_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'clip.mp4'
            path.write_bytes(b'a')
            first = V.CoachBateLoadH3VideoPath.IS_CHANGED(str(path))
            path.write_bytes(b'changed')
            self.assertNotEqual(first, V.CoachBateLoadH3VideoPath.IS_CHANGED(str(path)))

    def test_audio_tail_no_setting_is_identity(self):
        audio = object()
        self.assertIs(V.CoachBateShotAudioTail().apply(audio, '{"shot":{}}')[0], audio)

    def test_audio_tail_delegates_the_existing_recipe_unchanged(self):
        original = object()
        mocks = {key: Mock(FUNCTION='execute') for key in ('TrimAudioDuration', 'EmptyAudio', 'AudioConcat')}
        mocks['TrimAudioDuration'].execute.return_value = ('speech',)
        mocks['EmptyAudio'].execute.return_value = ('silence',)
        mocks['AudioConcat'].execute.return_value = ('joined',)
        nodes = {key: (lambda node=node: node) for key, node in mocks.items()}
        payload = json.dumps({'shot': {'output_audio_tail': {
            'keep_seconds': 12.5, 'silence_seconds': 5.5, 'sample_rate': 32000, 'channels': 2}}})
        with patch.dict(sys.modules, {'nodes': types.SimpleNamespace(NODE_CLASS_MAPPINGS=nodes)}):
            self.assertEqual(V.CoachBateShotAudioTail().apply(original, payload), ('joined',))
        mocks['TrimAudioDuration'].execute.assert_called_once_with(audio=original, start_index=0, duration=12.5)
        mocks['EmptyAudio'].execute.assert_called_once_with(duration=5.5, sample_rate=32000, channels=2)
        mocks['AudioConcat'].execute.assert_called_once_with(audio1='speech', audio2='silence', direction='after')

    def test_invalid_audio_tail_is_not_applied(self):
        for tail in ({}, {'keep_seconds': 0, 'silence_seconds': 2, 'sample_rate': 32000, 'channels': 2}):
            with self.assertRaises(ValueError):
                V.CoachBateShotAudioTail().apply(object(), json.dumps({'shot': {'output_audio_tail': tail}}))


if __name__ == '__main__':
    unittest.main()
