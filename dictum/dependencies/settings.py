import typing as t

import fastapi

from dictum.settings import Settings
from dictum.settings import get_settings

SettingsDep = t.Annotated[Settings, fastapi.Depends(get_settings)]
