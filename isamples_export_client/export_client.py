import datetime
import json
import logging
import time
from enum import Enum
import os
import os.path
from pathlib import Path
from typing import Optional, Any

import requests
from requests import Session, Response

from isamples_export_client.duckdb_utilities import (
    GeoFeaturesResult,
    TemporalExtent,
    load_db_from_jsonl,
    read_geo_features,
    get_temporal_extent
)
from isamples_export_client.geoparquet_utilities import write_geoparquet_from_json_lines

GEOPARQUET = "geoparquet"
START_TIME = "start_time"
EXPORT_SERVER_URL = "export_server_url"
FORMAT = "format"
QUERY = "query"
IS_GEOPARQUET = "is_geoparquet"

SOLR_INDEX_UPDATED_TIME = "indexUpdatedTime"

SOLR_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

STAC_COLLECTION_TYPE = "Collection"
STAC_CATALOG_TYPE = "Catalog"
STAC_VERSION = "1.0.0"
STAC_DEFAULT_LICENSE = "CC-BY-4.0"  # https://spdx.org/licenses/CC-BY-4.0.html
COLLECTION_ID = "isamples-stac-collection-"
COLLECTION_DESCRIPTION = """The Internet of Samples (iSamples) is a multi-disciplinary and multi-institutional
project funded by the National Science Foundation to design, develop, and promote service infrastructure to uniquely,
consistently, and conveniently identify material samples, record metadata about them, and persistently link them to
other samples and derived digital content, including images, data, and publications."""
COLLECTION_TITLE = "iSamples Stac Collection"
COLLECTION_LICENSE = "CC-BY-4.0"


def datetime_to_solr_format(dt):
    if dt is None:
        return None
    return dt.strftime(SOLR_TIME_FORMAT)


class JsonDateTimeEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, datetime.datetime):
            return o.isoformat(timespec="seconds")
        return json.JSONEncoder.default(self, o)


class ExportJobStatus(Enum):
    CREATED = "created"
    STARTED = "started"
    COMPLETED = "completed"
    ERROR = "error"

    @staticmethod
    def string_to_enum(raw_string: str) -> "ExportJobStatus":
        for enum_value in ExportJobStatus:
            if enum_value.value == raw_string:
                return enum_value
        raise ValueError(f"No ExportJobStatus found for {raw_string}")


def _is_expected_response_code(response: Response):
    return 200 <= response.status_code < 300


class ExportClient:
    def __init__(self, query: str,
                 destination_directory: str,
                 jwt: str,
                 export_server_url: str,
                 format: str,
                 collection_title: str,
                 collection_description: Optional[str] = None,
                 refresh_date: Optional[str] = None,
                 session: Session = requests.session(),
                 sleep_time: float = 5):
        self._max_errors = 3  # Max number of errors in retrieval loop
        self._query = query
        self._destination_directory = destination_directory
        self._jwt = jwt
        if not export_server_url.endswith("/"):
            export_server_url = f"{export_server_url}/"
        self._export_server_url = export_server_url
        if format == "geoparquet":
            self._format = "jsonl"
            self.is_geoparquet = True
        else:
            self._format = format
            self.is_geoparquet = False
        self._refresh_date = refresh_date
        self._rsession = session
        self._sleep_time = sleep_time
        self._collection_title = collection_title
        self._collection_description = collection_description

        try:
            os.makedirs(name=self._destination_directory, exist_ok=True)
        except OSError as e:
            raise ValueError(f"Unable to create export directory at {self._destination_directory}, error: {e}")

    @classmethod
    def from_existing_download(cls, refresh_dir: str, jwt: str) -> "ExportClient":
        manifest_file_path = ExportClient._manifest_file_path(refresh_dir)
        if not os.path.exists(manifest_file_path):
            raise ValueError(f"Refresh option was specified, but manifest file at {manifest_file_path} does not exist")
        with open(manifest_file_path, "r") as existing_file:
            manifest_list = json.load(existing_file)
            last_manifest_dict = manifest_list[-1]
            query = last_manifest_dict[QUERY]
            export_server_url = last_manifest_dict[EXPORT_SERVER_URL]
            format = last_manifest_dict[FORMAT]
            is_geoparquet = last_manifest_dict[IS_GEOPARQUET]
            if is_geoparquet:
                format = GEOPARQUET
            refresh_date = last_manifest_dict[START_TIME]
            return ExportClient(query, refresh_dir, jwt, export_server_url, format, refresh_date)

    @classmethod
    def _manifest_file_path(cls, dir_path: str):
        return os.path.join(dir_path, "manifest.json")

    @classmethod
    def _stac_item_path(cls, dir_path: str):
        return os.path.join(dir_path, "stac.json")

    @classmethod
    def _stac_catalog_path(cls, dir_path: str):
        return os.path.join(dir_path, "stac.json")

    def _authentication_headers(self) -> dict:
        return {
            "authorization": f"Bearer {self._jwt}"
        }

    def _query_with_timestamp(self) -> str:
        if self._refresh_date is not None:
            return f"{self._query} AND {SOLR_INDEX_UPDATED_TIME}:[{self._refresh_date} TO *]"
        else:
            return self._query

    def create(self) -> str:
        """Create a new export job, and return the uuid associated with the job"""
        query = self._query
        if self._refresh_date is not None:
            query = self._query_with_timestamp()

        params = {"q": query, "export_format": self._format}
        create_url = f"{self._export_server_url}create"
        response = self._rsession.get(create_url, headers=self._authentication_headers(), params=params)
        if _is_expected_response_code(response):
            json = response.json()
            return json.get("uuid")
        raise ValueError(f"Invalid response to export creation: {response.json()}")

    def status(self, uuid: str) -> Any:
        """Check the status of the specified export job"""
        status_url = f"{self._export_server_url}status?uuid={uuid}"
        response = self._rsession.get(status_url, headers=self._authentication_headers())
        if _is_expected_response_code(response):
            logging.info(response)
            return response.json()
        raise ValueError(f"Invalid response to export status: {response.json()}")

    def download(self, uuid: str) -> str:
        """Download the exported result set to the specified destination"""
        download_url = f"{self._export_server_url}download?uuid={uuid}"
        with self._rsession.get(download_url, stream=True, headers=self._authentication_headers()) as r:
            r.raise_for_status()
            current_time = datetime.datetime.now()
            date_string = current_time.strftime("%Y_%m_%d_%H_%M_%S")
            export_dir = os.path.join(self._destination_directory, date_string)
            try:
                os.makedirs(name=export_dir, exist_ok=True)
            except OSError as e:
                raise ValueError(f"Unable to create export directory at {export_dir}, error: {e}")
            filename = f"isamples_export_{date_string}.{self._format}"
            local_filename = os.path.join(export_dir, filename)
            with open(local_filename, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            return local_filename

    def write_manifest(self, query: str, uuid: str, tstarted: datetime.datetime, num_results: int, dir: str) -> str:
        new_manifest_dict = {
            QUERY: query,
            "uuid": uuid,
            FORMAT: self._format,
            START_TIME: datetime_to_solr_format(tstarted),
            "num_results": num_results,
            EXPORT_SERVER_URL: self._export_server_url,
            IS_GEOPARQUET: self.is_geoparquet
        }
        if self._refresh_date is not None:
            # if we are refreshing, include the additional timestamp filter for verbosity's sake
            new_manifest_dict["query_with_timestamp"] = self._query_with_timestamp()
        manifest_path = ExportClient._manifest_file_path(dir)
        if os.path.exists(manifest_path):
            with open(manifest_path, "r") as file:
                manifests = json.load(file)
            manifests.append(new_manifest_dict)
        else:
            manifests = [new_manifest_dict]
        with open(manifest_path, "w") as f:
            f.write(json.dumps(manifests, indent=4))
        return manifest_path

    def _gather_contained_stac_items(self, destination_directory: str) -> list[Path]:
        start_path = Path(destination_directory)
        return list(start_path.rglob("stac.json"))

    def write_stac_catalog(self) -> str:
        stac_catalog_path = ExportClient._stac_catalog_path(self._destination_directory)
        catalog_filename = os.path.basename(stac_catalog_path)
        links = [
            {
                "rel": "self",
                "type": "application/json",
                "href": catalog_filename
            },
            {
                "rel": "root",
                "type": "application/json",
                "href": catalog_filename
            },
        ]
        stac_items = self._gather_contained_stac_items(self._destination_directory)
        for item in stac_items:
            relative_path = os.path.relpath(item, self._destination_directory)
            if relative_path == "stac.json":
                continue
            dirname = os.path.basename(os.path.dirname(item))
            stac_item_link = {
                "rel": "child",
                "type": "application/json",
                "title": dirname,
                "href": relative_path
            }
            links.append(stac_item_link)

        title = self._collection_title or "iSamples STAC Catalog"
        description = self._collection_description or "STAC Catalog from iSamples Exports"

        stac_catalog = {
            "type": STAC_CATALOG_TYPE,
            # TODO: this needs to be a UUID for the whole catalog.  Where does that come from?
            "id": "iSamples Catalog",
            "title": title,
            "description": description,
            "stac_version": STAC_VERSION,
            # TODO: what's this?
            "conformsTo": "conformsTo",
            "links": links
        }

        with open(stac_catalog_path, "w", encoding="UTF-8") as f:
            json.dump(stac_catalog, f, indent=4, ensure_ascii=False, cls=JsonDateTimeEncoder)

        return stac_catalog_path

    def write_stac_item(
            self,
            uuid: str,
            tstarted: datetime.datetime,
            geo_result: GeoFeaturesResult,
            temporal_result: TemporalExtent,
            solr_query: str,
            json_file_path: str,
            parquet_file_path: str) -> str:

        assets_dict = {
        }
        description_string = self._collection_description or (
            f"iSamples Export Service results intiated at {tstarted}.  The solr query that produced this collection was  \n"
            f"```{solr_query}```.  \n"
        )
        title_string = self._collection_title or f"{COLLECTION_TITLE} {uuid}"
        if self.is_geoparquet:
            assets_dict["data"] = {
                "href": f"./{os.path.basename(parquet_file_path)}",
                "type": "application/x-parquet",
                "title": f"{COLLECTION_TITLE} {uuid} parquet export",
                "roles": [
                    "data"
                ],
                "description": "GeoParquet representation of the collection.",
                "alternate": {
                    "view": {
                        "title": "View parquet file",
                        "href": f"/ui/ds_view.html#/data/{os.path.relpath(parquet_file_path, self._destination_directory)}"
                    }
                }
            }
        stac_item = {
            "stac_version": STAC_VERSION,
            "stac_extensions": [
                "https://stac-extensions.github.io/table/v1.2.0/schema.json",
                "https://stac-extensions.github.io/alternate-assets/v1.1.0/schema.json"
            ],
            "type": STAC_COLLECTION_TYPE,
            "id": f"iSamples Export Service result {uuid}",
            "collection": title_string,
            "license": STAC_DEFAULT_LICENSE,
            "extent": {
                "spatial": {
                    "bbox": [geo_result.bbox,]
                },
                "temporal": {
                    "interval": [
                        temporal_result
                    ]
                }
            },
            "properties": {
                "datetime": datetime_to_solr_format(tstarted)
            },
            "description": description_string,
            "links": [
                {
                    "rel": "self",
                    "href": f"./{os.path.basename(json_file_path)}",
                    "type": "application/jsonl",
                    "title": f"{COLLECTION_TITLE} {uuid}",
                }
            ],
            # Columns adapted from https://raw.githubusercontent.com/isamplesorg/metadata/main/src/schemas/iSamplesSchemaCore1.0.json
            "table:columns": [
                {
                    "name": "sample_identifier",
                    "description": "URI that identifies the physical sample described by this record",
                    "type": "string"
                },
                {
                    "name": "label",
                    "description": "a human intelligible string used to identify a thing, i.e. the name to use for the thing; should be unique in the scope of a sample collection or dataset.",
                    "type": "string"
                },
                {
                    "name": "description",
                    "description": "Free text description of the subject of a triple.",
                    "type": "string"
                },
                {
                    "name": "alternate_identifiers",
                    "description": "one or more identifiers used to identify the sample in other contexts. In this context, the identifier property and scheme_name should be required.",
                    "type": "array"
                },
                {
                    "name": "produced_by",
                    "description": "object that documents the sampling event--who, where, when the specimen was obtained",
                    "type": "string"
                },
                {
                    "name": "sampling_purpose",
                    "description": "term to specify why a sample was collection.",
                    "type": "string"
                },
                {
                    "name": "has_context_category",
                    "description": "Top level context, based on the kind of feature sampled. Specific identification of the sampled feature of interest is done through the SamplingEvent/Feature of Interest property. At least one value is an instance of skos:Concept from the iSamples sampledfeaturevocabulary.",
                    "type": "array"
                },
                {
                    "name": "has_material_category",
                    "description": "The kind of material that constitutes the sample.  At least one value is an instance of skos:Concept from the iSamples MaterialTypeVocabulary; extension vocabularies can be used for more precise categorization.",
                    "type": "array"
                },
                {
                    "name": "has_specimen_category",
                    "description": "The kind of object the specimen is. At least one value is an instance of skos:Concept from the iSamples SpecimenTypeVocabulary; extension vocabularies can be used for more precise categorization.",
                    "type": "array"
                },
                {
                    "name": "keywords",
                    "description": "free text terms or formal categories associate with sample to support discovery. As in DataCite metadata, each keyword is a separate element. Multiple keywords should NOT be included as a comma-delimited list.",
                    "type": "array"
                },
                {
                    "name": "related_resource",
                    "description": "link to related resource with relationship property to indicate nature of connection. Target should be identifier for a resource.",
                    "type": "array"
                },
                {
                    "name": "complies_with",
                    "description": "a list of policies, recommendations, best practices (etc.) that have been followed in the collection and curation of the sample.",
                    "type": "array"
                },
                {
                    "name": "dc_rights",
                    "description": "a statement about various property rights associated with the resource, including intellectual property rights. Recommended practice is to refer to a rights statement with a URI. If this is not possible or feasible, a literal value (name, label, or short text) may be provided.",
                    "type": "string"
                },
                {
                    "name": "curation",
                    "description": "Information about the current storage of sample, access to sample, and events in curation history. Curation as used here starts when the sample is removed from its original context, and might include various processing steps for preservation.  Processing related to analysis preparation such as crushing, dissolution, evaporation, filtering are considered part of the sampling method for the derived child sample.",
                    "type": "string"
                },
                {
                    "name": "registrant",
                    "description": "identification of the agent that registered the sample, with contact information. Should include person name and affiliation, or position name and affiliation, or just organization name. e-mail address is preferred contact information.",
                    "type": "string"
                }
            ],
            "assets": assets_dict
        }
        stac_directory = os.path.dirname(json_file_path)
        stac_path = ExportClient._stac_item_path(stac_directory)
        with open(stac_path, "w", encoding="UTF-8") as f:
            json.dump(stac_item, f, indent=4, ensure_ascii=False, cls=JsonDateTimeEncoder)
        return stac_path

    def perform_full_download(self):
        logging.info("Contacting the export service to start the export process")
        tstarted = datetime.datetime.now()
        uuid = self.create()
        logging.info(f"Contacted the export service, created export job with uuid {uuid}")
        error_count = 0
        while True:
            try:
                status_json = self.status(uuid)
                status = ExportJobStatus.string_to_enum(status_json.get("status"))
                if status == ExportJobStatus.ERROR:
                    logging.info(f"Export job failed with error.  Check that your solr query is valid and try again.  Response: {status_json}")
                    break
                if status != ExportJobStatus.COMPLETED:
                    time.sleep(self._sleep_time)
                    logging.info(f"Export job still running, sleeping for {self._sleep_time} seconds")
                    continue
                else:
                    logging.info("Export job completed, going to download")
                    filename = self.download(uuid)
                    logging.info(f"Successfully downloaded file to {filename}")
                    num_results = sum(1 for _ in open(filename))
                    containing_directory = os.path.dirname(filename)
                    manifest_path = self.write_manifest(self._query, uuid, tstarted, num_results, containing_directory)
                    logging.info(f"Successfully wrote manifest file to {manifest_path}")
                    con = load_db_from_jsonl(filename)
                    geo_result = read_geo_features(con)
                    temporal_result = get_temporal_extent(con)
                    parquet_filename = None
                    if self.is_geoparquet:
                        parquet_filename = write_geoparquet_from_json_lines(filename)
                    print(status_json)
                    solr_query_dict = json.loads(status_json.get("query"))
                    query = solr_query_dict.pop("q")
                    stac_path = self.write_stac_item(uuid, tstarted, geo_result, temporal_result, query, filename, parquet_filename)
                    logging.info(f"Successfully wrote stac item to {stac_path}")
                    stac_catalog = self.write_stac_catalog()
                    logging.info(f"Successfully wrote stac catalog to {stac_catalog}")
                    break
            except Exception as e:
                logging.error("An error occurred:", e)
                # Sleep for a short time before retrying after an error
                time.sleep(self._sleep_time)
                error_count += 1
            if error_count > self._max_errors:
                logging.error("Maximum number of errors exceeded (%s/%s).", error_count, self._max_errors)
                break
