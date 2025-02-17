from isamples_export_client.duckdb_utilities import (
    load_db_from_jsonl,
    read_geo_features,
    get_temporal_extent
)


def test_duckdb_read_geo_features():
    con = load_db_from_jsonl("./test_data/isamples_export_2024_05_08_08_57_12.jsonl")
    geofeatures_result = read_geo_features(con)
    assert geofeatures_result is not None
    assert len(geofeatures_result.bbox) == 4
    geo_json_dict = geofeatures_result.geo_json_dict
    assert len(geo_json_dict) > 0
    assert geo_json_dict["type"] == "Polygon"
    assert geo_json_dict["coordinates"] is not None


def test_duckdb_read_temporal_extent():
    con = load_db_from_jsonl("./test_data/isamples_export_2024_05_08_08_57_12.jsonl")
    temporal_extent = get_temporal_extent(con)
    assert temporal_extent is not None
    assert len(temporal_extent) == 2
    assert temporal_extent[0] is not None
    assert temporal_extent[1] is not None
