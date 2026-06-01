import os.path
import sys

_search_path_env_str = os.environ.get("ADUSK_DATA")
env_search_paths = tuple(_search_path_env_str.split(":")) if _search_path_env_str is not None else ()

cfg_search_paths = (
    "~/.config/adusk/",
    "~/.adusk/",
    "/etc/adusk/",
)

static_search_paths = (
    "{}/share/adusk/".format(sys.prefix),
)


def find_cfg_resource(name):
    for path in env_search_paths:
        fullpath = "{}/cfg/{}".format(os.path.expanduser(path), name)
        if os.path.exists(fullpath):
            return fullpath
    for path in cfg_search_paths:
        fullpath = "{}/{}".format(os.path.expanduser(path), name)
        if os.path.exists(fullpath):
            return fullpath
    for path in static_search_paths:
        fullpath = "{}/cfg/{}".format(os.path.expanduser(path), name)
        if os.path.exists(fullpath):
            return fullpath
    return None


def find_data_resource(name):
    for path in env_search_paths:
        fullpath = "{}/{}".format(os.path.expanduser(path), name)
        if os.path.exists(fullpath):
            return fullpath
    for path in static_search_paths:
        fullpath = "{}/{}".format(os.path.expanduser(path), name)
        if os.path.exists(fullpath):
            return fullpath
    return None
