"""Read pass configuration and render only non-secret operator metadata."""
import argparse
import json
from common import deployment, operator_values, write_operator


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', help='pass:ENTRY or a legacy JSON file')
    parser.add_argument('--write-operator', metavar='PATH', help='Render non-secret local management metadata')
    args = parser.parse_args()
    config = deployment(args.config)
    if args.write_operator:
        if not write_operator(config, args.write_operator):
            raise ValueError('Operator metadata can only be generated from a pass configuration')
        print(json.dumps({'operator_metadata_written': True}))
    else:
        print(json.dumps(operator_values(config)))


if __name__ == '__main__':
    main()
