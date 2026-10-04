import pathlib
import yaml

config = yaml.safe_load(pathlib.Path('/opt/home-gateway/config/gateway.yaml').read_text())
print(config['mihomo'].get('secret', ''))
