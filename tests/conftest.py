import os
from pathlib import Path

os.environ.setdefault(
    "ADDON_WWW_DIR",
    str(Path(__file__).resolve().parent.parent / "hikvision_recordings" / "www"),
)

import pytest

SEARCH_OK = """<?xml version="1.0" encoding="UTF-8"?>
<CMSearchResult xmlns="http://www.hikvision.com/ver20/XMLSchema">
<searchID>0f4e6d1a-2b3c-4d5e-8f90-123456789abc</searchID>
<responseStatus>true</responseStatus>
<responseStatusStrg>OK</responseStatusStrg>
<numOfMatches>2</numOfMatches>
<matchList>
<searchMatchItem>
<sourceID>{00000000-0000-0000-0000-000000000000}</sourceID>
<trackID>101</trackID>
<timeSpan><startTime>2026-07-24T13:20:29Z</startTime><endTime>2026-07-24T13:21:42Z</endTime></timeSpan>
<mediaSegmentDescriptor><contentType>video</contentType><codecType>H.264-BP</codecType>
<playbackURI>rtsp://10.10.11.56/Streaming/tracks/101/?starttime=20260724T132029Z&amp;endtime=20260724T132142Z&amp;name=00000000002000001&amp;size=32947084</playbackURI>
</mediaSegmentDescriptor>
</searchMatchItem>
<searchMatchItem>
<trackID>101</trackID>
<timeSpan><startTime>2026-07-24T13:25:00Z</startTime><endTime>2026-07-24T13:26:00Z</endTime></timeSpan>
<mediaSegmentDescriptor>
<playbackURI>rtsp://10.10.11.56/Streaming/tracks/101/?starttime=20260724T132500Z&amp;endtime=20260724T132600Z&amp;name=00000000002000002&amp;size=1048576</playbackURI>
</mediaSegmentDescriptor>
</searchMatchItem>
</matchList>
</CMSearchResult>"""

SEARCH_NO_MATCH = """<?xml version="1.0" encoding="UTF-8"?>
<CMSearchResult xmlns="http://www.hikvision.com/ver20/XMLSchema">
<searchID>0f4e6d1a-2b3c-4d5e-8f90-123456789abc</searchID>
<responseStatus>true</responseStatus>
<responseStatusStrg>NO MATCHES</responseStatusStrg>
<numOfMatches>0</numOfMatches>
<matchList></matchList>
</CMSearchResult>"""

BAD_XML_CONTENT = """<?xml version="1.0" encoding="UTF-8"?>
<ResponseStatus xmlns="http://www.hikvision.com/ver20/XMLSchema">
<requestURL>/ISAPI/ContentMgmt/search</requestURL>
<statusCode>6</statusCode>
<statusString>Invalid XML Content</statusString>
<subStatusCode>badXmlContent</subStatusCode>
</ResponseStatus>"""

DEVICE_TIME = """<?xml version="1.0" encoding="UTF-8"?>
<Time xmlns="http://www.hikvision.com/ver20/XMLSchema">
<timeMode>manual</timeMode>
<localTime>2026-07-24T13:20:29-05:00</localTime>
<timeZone>CST6CDT,M3.2.0/02:00:00,M11.1.0/02:00:00</timeZone>
</Time>"""


@pytest.fixture
def search_ok() -> str:
    return SEARCH_OK


@pytest.fixture
def search_no_match() -> str:
    return SEARCH_NO_MATCH


@pytest.fixture
def bad_xml_content() -> str:
    return BAD_XML_CONTENT


@pytest.fixture
def device_time() -> str:
    return DEVICE_TIME
