# coding: utf-8
from xml.etree import ElementTree
from pathlib import Path
import os
from osgeo import osr
import dateutil
from dateutil import parser
from datetime import timedelta
import uuid
import yaml
import logging
import click
import re
import boto3
import datacube
from datacube.scripts.dataset import create_dataset, parse_match_rules_options
from datacube.utils import changes

MTL_PAIRS_RE = re.compile(r'(\w+)\s=\s(.*)')

bands_ls8 = [('1', 'coastal_aerosol'),
             ('2', 'blue'),
             ('3', 'green'),
             ('4', 'red'),
             ('5', 'nir'),
             ('6', 'swir1'),
             ('7', 'swir2'),
             ('8', 'panchromatic'),
             ('9', 'cirrus'),
             ('10', 'lwir1'),
             ('11', 'lwir2'),
             ('QUALITY', 'quality')]

bands_ls7 = [('1', 'blue'),
             ('2', 'green'),
             ('3', 'red'),
             ('4', 'nir'),
             ('5', 'swir1'),
             ('7', 'swir2'),
             ('QUALITY', 'quality')]


def _parse_value(s):
    s = s.strip('"')
    for parser in [int, float]:
        try:
            return parser(s)
        except ValueError:
            pass
    return s


def _parse_group(lines):
    tree = {}
    for line in lines:
        match = MTL_PAIRS_RE.findall(line)
        if match:
            key, value = match[0]
            if key == 'GROUP':
                tree[value] = _parse_group(lines)
            elif key == 'END_GROUP':
                break
            else:
                tree[key] = _parse_value(value)
    return tree


def get_geo_ref_points(info):
    return {
        'ul': {'x': info['CORNER_UL_PROJECTION_X_PRODUCT'], 'y': info['CORNER_UL_PROJECTION_Y_PRODUCT']},
        'ur': {'x': info['CORNER_UR_PROJECTION_X_PRODUCT'], 'y': info['CORNER_UR_PROJECTION_Y_PRODUCT']},
        'll': {'x': info['CORNER_LL_PROJECTION_X_PRODUCT'], 'y': info['CORNER_LL_PROJECTION_Y_PRODUCT']},
        'lr': {'x': info['CORNER_LR_PROJECTION_X_PRODUCT'], 'y': info['CORNER_LR_PROJECTION_Y_PRODUCT']},
    }


def get_coords(geo_ref_points, spatial_ref):
    t = osr.CoordinateTransformation(spatial_ref, spatial_ref.CloneGeogCS())

    def transform(p):
        lon, lat, z = t.TransformPoint(p['x'], p['y'])
        return {'lon': lon, 'lat': lat}

    return {key: transform(p) for key, p in geo_ref_points.items()}


def satellite_ref(sat):
    """
    To load the band_names for referencing either LANDSAT8 or LANDSAT7 bands
    """
    if sat == 'LANDSAT_8':
        sat_img = bands_ls8
    elif sat == 'LANDSAT_7' or sat == 'LANDSAT_5':
        sat_img = bands_ls7
    else:
        raise ValueError('Not Landsat 8 or Landsat 7')
    return sat_img


def get_product_type(file):
    """Check whether the data is collection 1 or pre collection """
    data_type = file.split('_')
    if len(data_type) == 2:
        product_type = 'Pre_Collection_L1'
    else:
        product_type = 'Level1'
    return product_type


def format_obj_key(obj_key):
    obj_key = '/'.join(obj_key.split("/")[:-1])
    return obj_key


def get_s3_url(bucket_name, obj_key):
    return 'http://{bucket_name}.s3.amazonaws.com/{obj_key}'.format(
        bucket_name=bucket_name, obj_key=obj_key)


def absolutify_paths(doc, bucket_name, obj_key):
    objt_key = format_obj_key(obj_key)
    for band in doc['image']['bands'].values():
        band['path'] = get_s3_url(bucket_name, objt_key + '/' + band['path'])
    return doc


def make_metadata_doc(mtl_data, bucket_name, object_key):
    mtl_product_info = mtl_data['PRODUCT_METADATA']
    mtl_metadata_info = mtl_data['METADATA_FILE_INFO']
    satellite = mtl_product_info['SPACECRAFT_ID']
    instrument = mtl_product_info['SENSOR_ID']
    acquisition_date = mtl_product_info['DATE_ACQUIRED']
    scene_center_time = mtl_product_info['SCENE_CENTER_TIME']
    level = mtl_product_info['DATA_TYPE']
    file_name = mtl_product_info['METADATA_FILE_NAME']
    product_type = 'level1'
    output_format = mtl_product_info['OUTPUT_FORMAT']
    sensing_time = acquisition_date + ' ' + scene_center_time
    cs_code = 32600 + mtl_data['PROJECTION_PARAMETERS']['UTM_ZONE']
    label = mtl_metadata_info['LANDSAT_SCENE_ID']
    groundstation = mtl_metadata_info['STATION_ID']
    spatial_ref = osr.SpatialReference()
    spatial_ref.ImportFromEPSG(cs_code)
    geo_ref_points = get_geo_ref_points(mtl_product_info)
    coordinates = get_coords(geo_ref_points, spatial_ref)
    bands = satellite_ref(satellite)
    doc = {
        'id': str(uuid.uuid5(uuid.NAMESPACE_URL, get_s3_url(bucket_name, object_key))),
        'processing_level': level,
        'product_type': product_type,
        'creation_dt': str(acquisition_date),
        'label': label,
        'platform': {'code': satellite},
        'instrument': {'name': instrument},
        'acquisition': {
            'groundstation': {
                'code': groundstation}
        },
        'extent': {
            'from_dt': sensing_time,
            'to_dt': sensing_time,
            'center_dt': sensing_time,
            'coord': coordinates,
        },
        'format': {'name': 'GeoTiff'},
        'grid_spatial': {
            'projection': {
                'geo_ref_points': geo_ref_points,
                'spatial_reference': 'EPSG:%s' % cs_code,
            }
        },
        'image': {
            'bands': {
                band[1]: {
                    'path': mtl_product_info['FILE_NAME_BAND_' + band[0]],
                    'layer': 1,
                } for band in bands
            }
        },
        'lineage': {'source_datasets': {}},
    }
    doc = absolutify_paths(doc, bucket_name, object_key)
    return doc


def get_metadata_docs(bucket_name, prefix, start, stop):
    s3 = boto3.resource('s3')
    bucket = s3.Bucket(bucket_name)
    if prefix and start and stop is not None:
        start = start.split("/")
        stop = stop.split("/")
        for path in range(int(start[0]), int(stop[0]), -1):
            for row in range(int(start[1]), int(stop[1]), 1):
                prefix_new = (str(prefix) + '/' + str(format(path, '03d')) + '/' + str(format(row, '03d')))
                logging.info("Processing prefix: %s", prefix_new)
                for obj in bucket.objects.filter(Prefix=prefix_new):
                    if obj.key.endswith('MTL.txt') and (not obj.key.endswith('RT_MTL.txt')):
                        obj_key = obj.key
                        try:
                            raw_string = obj.get()['Body'].read().decode('utf8')
                            mtl_doc = _parse_group(iter(raw_string.split("\n")))['L1_METADATA_FILE']
                            metadata_doc = make_metadata_doc(mtl_doc, bucket_name, obj_key)
                        except Exception as err:
                            logging.info("Error: %s: %s. Continuing...", err, obj_key)
                            continue
                        else:
                            yield obj_key, metadata_doc


def make_rules(index):
    all_product_names = [prod.name for prod in index.products.get_all()]
    rules = parse_match_rules_options(index, all_product_names, True)
    return rules

def add_dataset(doc, uri, rules, index):
    dataset = create_dataset(doc, uri, rules)

    try:
        index.datasets.add(dataset, sources_policy='skip')
    except changes.DocumentMismatchError as e:
        index.datasets.update(dataset, {tuple(): changes.allow_any})
    return uri


def add_datacube_dataset(bucket_name, config, prefix, start, stop):
    dc = datacube.Datacube(config=config)
    index = dc.index
    rules = make_rules(index)

    for metadata_path, metadata_doc in get_metadata_docs(bucket_name, prefix, start, stop):
        uri = get_s3_url(bucket_name, metadata_path)
        add_dataset(metadata_doc, uri, rules, index)
        logging.info("Indexing %s", metadata_path)


@click.command(help="Enter Bucket name. Optional to enter configuration file to access a different database")
@click.argument('bucket_name')
@click.option('--config', '-c', help=" Pass the config file to access the database", type=click.Path(exists=True))
@click.option('--prefix', '-p', help="Pass the prefix of the object to the bucket")
@click.option('--start', '-s', default=None, help="Pass the path and row for the object to start from eg: 134/76")
@click.option('--stop', '-f', default=None, help="Pass {path}/{row} to create a box to index eg: 123/90")
def main(bucket_name, config, prefix, start, stop):
    logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.INFO)
    add_datacube_dataset(bucket_name, config, prefix, start, stop)


if __name__ == "__main__":
    main()
