from shorts import scene_planner, qc


def test_clip_offset_changes_approved_version(tmp_path):
    (tmp_path / 'clip.mp4').write_bytes(b'video')
    scene = dict(text='Текст', start=0, end=3,
                 asset=dict(type='clip', path='clip.mp4', offset=0))
    before = scene_planner.scene_hash(scene, tmp_path)
    scene['asset']['offset'] = 5
    assert before != scene_planner.scene_hash(scene, tmp_path)


def test_changed_middle_of_large_file_changes_version(tmp_path):
    path = tmp_path / 'clip.mp4'
    path.write_bytes(b'a' * (4 * 1024 * 1024))
    scene = dict(text='Текст', start=0, end=3, asset=dict(type='clip', path='clip.mp4'))
    before = scene_planner.scene_hash(scene, tmp_path)
    with path.open('r+b') as file:
        file.seek(2 * 1024 * 1024)
        file.write(b'changed')
    assert before != scene_planner.scene_hash(scene, tmp_path)


def test_qc_accepts_selected_720p_profile():
    probe = dict(format={'duration':'88'}, streams=[dict(codec_type='video',width=720,height=1280),
                                                  dict(codec_type='audio')])
    assert not qc.parse(probe, '', vertical=True, expected_size=(720,1280))


def test_same_uploaded_video_under_two_names_is_used_once(tmp_path):
    import asyncio
    from shorts import remix
    first, second = tmp_path / 'a.mp4', tmp_path / 'b.mp4'
    first.write_bytes(b'same media')
    second.write_bytes(b'same media')
    assert asyncio.run(remix.unique_assets([first, second])) == [first]
