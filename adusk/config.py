import yaml

from adusk import resources


class ObjectConfig:
    objects = {}

    def set_object(self, name, data):
        self.objects[name] = data

    def construct(self): pass


class YamlFile:
    def __init__(self, filename):
        self.filename = filename
        self.file_path = resources.find_cfg_resource(filename)
        assert self.file_path is not None, "Could not find YAML file `{}`!".format(filename)
        print("Found YAML file at `{}`".format(self.file_path))
        self.yaml_data = {}

    def read(self):
        with open(self.file_path, 'r', encoding='utf-8') as file:
            self.yaml_data = yaml.safe_load(file)

    def add_to_config(self, key, object_config):
        assert key in self.yaml_data, "{} malformed! Could not find key `{}`".format(self.filename, key)
        object_config.set_object(key, self.yaml_data[key])
