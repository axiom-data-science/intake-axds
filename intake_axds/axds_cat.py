"""
Set up a catalog for Axiom assets.
"""


from typing import Dict, Optional, Union

import pandas as pd
import requests

from intake.catalog.base import Catalog
from intake.catalog.local import LocalCatalogEntry
from intake.source.csv import CSVSource
from intake_xarray.netcdf import NetCDFSource

from operator import itemgetter
from shapely import wkt

from . import __version__
from .utils import match_key_to_parameter, return_docs_response


search_headers = {"Accept": "application/json"}


class AXDSCatalog(Catalog):
    """
    Makes data sources out of all datasets for a given AXDS data type.

    Have this cover all data types for now, then split out.
    """

    name = "axds_cat"
    version = __version__

    def __init__(
        self,
        datatype: str = "platform2",
        outtype: str = "dataframe",
        keys_to_match: Optional[str] = None,
        kwargs_search: Optional[Dict[str, Union[str, int, float]]] = None,
        page_size: int = 10,
        verbose: bool = False,
        name: str = "catalog",
        description: str = "Catalog of Axiom assets.",
        metadata: Optional[Dict[str, Union[str, int, float]]] = None,
        ttl: Optional[int] = None,
        **kwargs,
    ):
        """Initialize an Axiom Catalog.

        Parameters
        ----------
        datatype : str
            Axiom data type. Currently only "platform2" but eventually also "module".
        outtype : str
            Type of output. Probably will be "dataframe" or "xarray".
        keys_to_match : str, optional
            Name of key to match with system-available variable parameterNames using criteria. Currently only 1 at a time.
        kwargs_search : dict, optional
            Contains search information if desired. Keys include: "max_lon", "max_lat", "min_lon", "min_lat", "min_time", "max_time". Longitude values should be in the range -180 to 180, which is the standard for the Axiom system.
        page_size : int, optional
            Number of results. Default is 1000 but fewer is faster.
        verbose : bool, optional
            Set to True for helpful information.
        ttl : int, optional
            Time to live for catalog. How long before force-reloading catalog. Set to None to not do this.
        name : str, optional
            Name for catalog.
        description : str, optional
            Description for catalog.
        metadata : dict, optional
            Metadata for catalog.
        kwargs: 
            Other input arguments are passed to the intake Catalog class. They can includegetenv, getshell, persist_mode, storage_options, and user_parameters, in addition to some that are surfaced directly in this class.
        """

        self.datatype = datatype
        self.url_docs_base = "https://search.axds.co/v2/docs?verbose=true"
        self.kwargs_search = kwargs_search
        self.page_size = page_size
        self.verbose = verbose

        if datatype == "module" and outtype == "dataframe":
            raise ValueError(
                "modules cannot be exported as dataframes since they are gridded data."
            )

        self.outtype = outtype

        if kwargs_search is not None:
            checks = [
                ["min_lon", "max_lon", "min_lat", "max_lat"],
                ["min_time", "max_time"],
            ]
            for check in checks:
                if any(key in kwargs_search for key in check) and not all(
                    key in kwargs_search for key in check
                ):
                    raise ValueError(
                        f"If any of {check} are input, they all must be input."
                    )
            if abs(kwargs_search["min_lon"]) > 180 or abs(kwargs_search["max_lon"]) > 180:
                raise ValueError("`min_lon` and `max_lon` must be in the range -180 to 180.")
        else:
            kwargs_search = {}
        self.kwargs_search = kwargs_search

        if keys_to_match is not None:
            # Currently just take first match, but there could be more than one.
            self.pglabel = match_key_to_parameter(keys_to_match)[0]
        else:
            self.pglabel = None
        
        # Put together catalog-level stuff
        if metadata is None:
            metadata = {}
            metadata["kwargs_search"] = self.kwargs_search
            metadata["pglabel"] = self.pglabel
            metadata["outtype"] = self.outtype

        super(AXDSCatalog, self).__init__(**kwargs, ttl=ttl, name=name,
                                          description=description,
                                          metadata=metadata)

    @property
    def search_url(self):
        """Set up url for search."""

        if not hasattr(self, "_search_url"):

            self.url_search_base = f"https://search.axds.co/v2/search?portalId=-1&page=1&pageSize={self.page_size}&verbose=true"

            url = f"{self.url_search_base}&type={self.datatype}"

            if self.kwargs_search.keys() >= {
                "max_lon",
                "min_lon",
                "min_lat",
                "max_lat",
            }:
                url_add_box = (
                    f'&geom={{"type":"Polygon","coordinates":[[[{self.kwargs_search["min_lon"]},{self.kwargs_search["min_lat"]}],'
                    + f'[{self.kwargs_search["max_lon"]},{self.kwargs_search["min_lat"]}],'
                    + f'[{self.kwargs_search["max_lon"]},{self.kwargs_search["max_lat"]}],'
                    + f'[{self.kwargs_search["min_lon"]},{self.kwargs_search["max_lat"]}],'
                    + f'[{self.kwargs_search["min_lon"]},{self.kwargs_search["min_lat"]}]]]}}'
                )
                url += f"{url_add_box}"

            if self.kwargs_search.keys() >= {"max_time", "min_time"}:
                # convert input datetime to seconds since 1970
                startDateTime = (
                    pd.Timestamp(self.kwargs_search["min_time"]).tz_localize("UTC")
                    - pd.Timestamp("1970-01-01 00:00").tz_localize("UTC")
                ) // pd.Timedelta("1s")
                endDateTime = (
                    pd.Timestamp(self.kwargs_search["max_time"]).tz_localize("UTC")
                    - pd.Timestamp("1970-01-01 00:00").tz_localize("UTC")
                ) // pd.Timedelta("1s")

                # search by time
                url_add_time = (
                    f"&startDateTime={startDateTime}&endDateTime={endDateTime}"
                )

                url += f"{url_add_time}"

            if self.pglabel is not None:

                # search by variable
                url += f"&tag=Parameter+Group:{self.pglabel}"

            # if requests.get(url).status_code != 200:
            #     raise ValueError("")

            if self.verbose:
                print(f"search url: {url}")

            self._search_url = url

        return self._search_url
    
    def _load_metadata(self, results: Dict[str, str]) -> dict:
        """Load metadata for catalog entry.
        
        Parameters
        ----------
        results : dict
            Returned results from call to server for a single dataset.
        
        Returns
        -------
        dict
            Metadata to store with catalog entry.
        """

        # matching names in intake-erddap
        keys = ["datasetID", "title", "summary", "type", "minTime", "maxTime"]
        # names of keys in Axiom system. 
        items = ["uuid", "label", "description", "type", "start_date_time", "end_date_time"]
        values = itemgetter(*items)(results)
        metadata = dict(zip(keys, values))

        items = ["institution", "geospatial_bounds"]
        values = itemgetter(*items)(results["source"]["meta"]["attributes"])
        metadata.update(dict(zip(items, values)))

        p1 = wkt.loads(metadata["geospatial_bounds"])
        keys = ["minLongitude", "minLatitude", "maxLongitude", "maxLatitude"]
        metadata.update(dict(zip(keys, p1.bounds)))
            
        metadata["variables"] = list(results["source"]["meta"]["variables"].keys())
        
        return metadata

    def _load(self):
        """Find all dataset ids and create catalog."""

        res = requests.get(self.search_url, headers=search_headers).json()

        if "results" not in res:
            raise ValueError("No results were returned for the search.")

        if self.verbose:
            print(f"Number of results found: {len(res['results'])}. Page size: {self.page_size}.")

        self._entries = {}

        for results in res["results"]:
            dataset_id = results["uuid"]
            
            # # quick check if OPENDAP is in the access methods for this uuid, otherwise move on
            # if self.datatype == "module":
            #     # if opendap is not in the access methods at the module level, then we assume it
            #     # also isn't at the layer_group level, so we will not check each layer_group
            #     if "OPENDAP" not in results["data"]["access_methods"]:
            #         if self.verbose:
            #             print(
            #                 f"Cannot access module {dataset_id} via opendap so no source is being made for it.",
            #                 UserWarning,
            #             )
            #         # warnings.warn(f"Cannot access module {dataset_id} via opendap so no source is being made for it.", UserWarning)
            #         continue
            #     if "DEPRECATED" in results["data"]["label"]:
            #         if self.verbose:
            #             print(
            #                 f"Skipping module {dataset_id} because label says it is deprecated.",
            #                 UserWarning,
            #             )
            #         continue

            description = f"AXDS dataset_id {dataset_id} of datatype {self.datatype}"

            # docs = return_docs_response(dataset_id)
            # Find urlpath
            if self.datatype == "platform2":
                # urlpath = results["source"]["files"]["data.csv.gz"]["url"]
                # urlpath = results["source"]["files"]["deployment.nc"]["url"]
                # urlpath = results["source"]["files"]["data.viz.parquet"]["url"]
                # url = f"{self.url_docs_base}&id={dataset_id}"
                # res2 = requests.get(url, headers=search_headers).json()
                if self.outtype == "dataframe":
                    # urlpath = docs["data"]["resources"]["files"]["data.csv.gz"]["url"]
                    urlpath = results["source"]["files"]["data.csv.gz"]["url"]
                    # urlpath = results["source"]["files"]["data.viz.parquet"]["url"]
                    plugin = CSVSource  # 'csv'
                elif self.outtype == "xarray":
                    # key = [
                    #     key
                    #     for key in docs["data"]["resources"]["files"].keys()
                    #     if ".nc" in key
                    # ][0]
                    # urlpath = docs["data"]["resources"]["files"][key]["url"]
                    urlpath = results["source"]["files"]["deployment.nc"]["url"]
                    plugin = NetCDFSource  # 'netcdf'
            # elif self.datatype == "module":
            #     plugin = NetCDFSource  # 'netcdf'

            #     # modules are the umbrella and contain 1 or more layer_groups
            #     # pull out associated layer groups uuids to make sure to capture them
            #     layer_group_uuids = list(docs["data"]["layer_group_info"].keys())

            #     # pull up docs for each layer_group to get urlpath
            #     # can only get a urlpath if it is available on opendap
            #     urlpaths = []  # using this to see if there are ever multiple urlpaths
            #     for layer_group_uuid in layer_group_uuids:
            #         docs_lg = return_docs_response(layer_group_uuid)

            #         if "OPENDAP" in docs_lg["data"]["access_methods"]:
            #             urlpath = docs_lg["source"]["layers"][0][
            #                 "thredds_opendap_url"
            #             ].removesuffix(".html")
            #             urlpaths.append(urlpath)

            #     # only want unique urlpaths
            #     urlpaths = list(set(urlpaths))
            #     if len(urlpaths) > 1:
            #         if self.verbose:
            #             print(
            #                 f"Several urlpaths were found for module {dataset_id} so no source is being made for it."
            #             )
            #         # warnings.warn(f"Several urlpaths were found for module {dataset_id} so no source is being made for it.", UserWarning)
            #         continue
            #         # raise ValueError(f"the layer_groups for module {dataset_id} have different urlpaths.")
            #     elif len(urlpaths) == 0:
            #         if self.verbose:
            #             print(
            #                 f"No urlpath was found for module {dataset_id} so no source is being made for it."
            #             )
            #         # warnings.warn(f"No urlpath was found for module {dataset_id} so no source is being made for it.", UserWarning)
            #         continue
            #     else:
            #         urlpath = urlpaths[0]
            #     # import pdb; pdb.set_trace()

            #     # # gather metadata — this is at the module level. Is it different by layer_group?
            #     # max_lat, min_lat = results["data"]["max_lat"], results["data"]["min_lat"]
            #     # max_lng, min_lng = results["data"]["max_lng"], results["data"]["min_lng"]
            #     # slug = results["data"]["model"]["slug"]
            #     # start_time, end_time = results["data"]["start_time_utc"], results["data"]["end_time_utc"]
            #     # # there are description and label too but are they the same for module and layer_group?

            args = {
                "urlpath": urlpath,
            }

            entry = LocalCatalogEntry(
                name=dataset_id,
                description=description,
                driver=plugin,
                direct_access="allow",
                args=args,
                metadata=self._load_metadata(results),
                # True,
                # args,
                # {},
                # {},
                # {},
                # "",
                # getenv=False,
                # getshell=False,
            )
            # entry._metadata = {
            #     # "info_url": f"{self.url_docs_base}&id={dataset_id}",
            #     "dataset_id": dataset_id,
            # }

            entry._plugin = [plugin]

            self._entries[dataset_id] = entry
