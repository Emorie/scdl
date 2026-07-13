from base64 import b64decode

from scdl.metadata_assembler import JPEG_MIME_TYPE, _assemble_vorbis_artwork_tags


def test_assemble_vorbis_artwork_tags_writes_compat_keys() -> None:
    tags: dict[str, object] = {}
    jpeg_data = b"jpeg-bytes"

    _assemble_vorbis_artwork_tags(tags, jpeg_data)

    assert "metadata_block_picture" in tags
    assert "coverart" in tags
    assert "coverartmime" in tags

    coverart = tags["coverart"]
    assert isinstance(coverart, list)
    assert b64decode(coverart[0]) == jpeg_data

    coverartmime = tags["coverartmime"]
    assert isinstance(coverartmime, list)
    assert coverartmime[0] == JPEG_MIME_TYPE
