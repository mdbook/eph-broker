"""assets.py: optional viewer access to the broker's pure shared transcript sources."""


def _shared():
    import transcript_assets
    return transcript_assets


def transcript_text():
    return _shared().transcript_text()


def transcript_renderer():
    return _shared().transcript_renderer()


def transcript_css():
    return _shared().transcript_css()


def transcript_module():
    return _shared().transcript_module()
