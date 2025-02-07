import time
import logging
from dataclasses import dataclass, field, asdict

import numpy as np
import xarray as xr
import matplotlib.pyplot as plt
import yaml
import importlib.metadata
from typing import Dict, Union, List
from roms_tools.setup.topography import _add_topography
from roms_tools.setup.mask import _add_mask, _add_velocity_masks
from roms_tools.vertical_coordinate import (
    sigma_stretch,
    compute_depth,
    add_depth_coordinates_to_dataset,
)
from roms_tools.plot import _plot, _section_plot
from roms_tools.setup.utils import (
    interpolate_from_rho_to_u,
    interpolate_from_rho_to_v,
    get_target_coords,
    gc_dist,
)
from roms_tools.setup.utils import extract_single_value, save_datasets
from pathlib import Path


@dataclass(frozen=True, kw_only=True)
class Grid:
    """A single ROMS grid, used for creating, plotting, and then saving a new ROMS
    domain grid.

    The grid generation consists of four steps:

    1.  Creating the horizontal grid
    2.  Creating the mask
    3.  Generating the topography
    4.  Preparing the vertical coordinate system

    Parameters
    ----------
    nx : int
        Number of grid points in the x-direction.
    ny : int
        Number of grid points in the y-direction.
    size_x : float
        Domain size in the x-direction (in kilometers).
    size_y : float
        Domain size in the y-direction (in kilometers).
    center_lon : float
        Longitude of grid center.
    center_lat : float
        Latitude of grid center.
    rot : float, optional
        Rotation of grid x-direction from lines of constant latitude, measured in degrees.
        Positive values represent a counterclockwise rotation.
        The default is 0, which means that the x-direction of the grid is aligned with lines of constant latitude.
    topography_source : Dict[str, Union[str, Path]], optional
        Dictionary specifying the source of the topography data:

        - "name" (str): The name of the topography data source (e.g., "SRTM15").
        - "path" (Union[str, Path, List[Union[str, Path]]]): The path to the raw data file. Can be a string or a Path object.

        The default is "ETOPO5", which does not require a path.
    hmin : float, optional
       The minimum ocean depth (in meters). The default is 5.0.
    N : int, optional
        The number of vertical levels. The default is 100.
    theta_s : float, optional
        The surface control parameter. Must satisfy 0 < theta_s <= 10. The default is 5.0.
    theta_b : float, optional
        The bottom control parameter. Must satisfy 0 < theta_b <= 4. The default is 2.0.
    hc : float, optional
        The critical depth (in meters). The default is 300.0.
    verbose: bool, optional
        Indicates whether to print grid generation steps with timing. Defaults to False.

    Raises
    ------
    ValueError
        If you try to create a grid with domain size larger than 20000 km.
    """

    nx: int
    ny: int
    size_x: float
    size_y: float
    center_lon: float
    center_lat: float
    rot: float = 0
    N: int = 100
    theta_s: float = 5.0
    theta_b: float = 2.0
    hc: float = 300.0
    topography_source: Dict[str, Union[str, Path, List[Union[str, Path]]]] = None
    hmin: float = 5.0
    verbose: bool = False
    ds: xr.Dataset = field(init=False, repr=False)
    straddle: bool = field(init=False, repr=False)

    def __post_init__(self):

        self._input_checks()

        # Horizontal grid
        self._create_horizontal_grid()

        # Check if the Greenwich meridian goes through the domain.
        self._straddle()

        # Mask
        self._create_mask(verbose=self.verbose)

        # Coarsen the dataset if needed
        self._coarsen()

        # Topography and mask
        self.update_topography(
            topography_source=self.topography_source,
            hmin=self.hmin,
            verbose=self.verbose,
        )

        # Vertical coordinate system
        self.update_vertical_coordinate(
            N=self.N,
            theta_s=self.theta_s,
            theta_b=self.theta_b,
            hc=self.hc,
            verbose=self.verbose,
        )

    def _input_checks(self):
        if self.topography_source is None:
            object.__setattr__(self, "topography_source", {"name": "ETOPO5"})

        if "name" not in self.topography_source:
            raise ValueError(
                "`topography_source` must include a 'name' key specifying the data source."
            )

        if self.topography_source["name"] != "ETOPO5":
            if "path" not in self.topography_source:
                raise ValueError(
                    "`topography_source` must include a 'path' key when the 'name' is not 'ETOPO5'."
                )

    def _create_mask(self, verbose=False) -> None:

        if verbose:
            start_time = time.time()
            logging.info("=== Creating the mask ===")
        ds = _add_mask(self.ds)

        if verbose:
            logging.info(f"Total time: {time.time() - start_time:.3f} seconds")
            logging.info(
                "========================================================================================================"
            )

        object.__setattr__(self, "ds", ds)

    def update_topography(
        self, topography_source=None, hmin=None, verbose=False
    ) -> None:
        """Update the grid dataset with processed topography.

        This method performs several key operations, including regridding the topography, smoothing the
        topography over the entire domain and locally.
        The processed topography is then added to the grid's dataset.

        Parameters
        ----------
        topography_source : dict, optional
            A dictionary specifying the source of the topography data. The dictionary should
            contain the following keys:
            - "name" (str): The name of the topography data source (e.g., "SRTM15").
            - "path" (Union[str, Path): The path to the raw data file.

            If not provided, `topography_source` will remain unchanged (i.e., the existing value will not be overwritten).

        hmin : float, optional
            The minimum ocean depth (in meters).
            If not provided, `hmin` will remain unchanged (i.e., the existing value will not be overwritten).

        verbose : bool, optional
            If True, the method will print detailed information about the grid generation process,
            including the timing of each step. Defaults to False.

        Returns
        -------
        None
            This method updates the internal dataset (`self.ds`) in place by adding or overwriting the
            topography variable. It does not return any value.
        """

        topography_source = topography_source or self.topography_source
        hmin = hmin or self.hmin

        # Extract target coordinates for processing
        target_coords = get_target_coords(self)

        # If verbose is enabled, start the timer and print the start message
        if verbose:
            start_time = time.time()
            logging.info(
                f"=== Generating the topography using {topography_source['name']} data and hmin = {hmin} meters ==="
            )

        # Add topography and mask to the dataset
        ds = _add_topography(
            ds=self.ds,
            target_coords=target_coords,
            topography_source=topography_source,
            hmin=hmin,
            verbose=verbose,
        )

        # If verbose is enabled, print elapsed time and a separator
        if verbose:
            logging.info(f"Total time: {time.time() - start_time:.3f} seconds")
            logging.info(
                "========================================================================================================"
            )

        # Update the grid's dataset and related attributes
        object.__setattr__(self, "ds", ds)
        object.__setattr__(self, "topography_source", topography_source)
        object.__setattr__(self, "hmin", hmin)

    def update_vertical_coordinate(
        self, N=None, theta_s=None, theta_b=None, hc=None, verbose=False
    ) -> None:
        """Create vertical coordinate variables for the ROMS grid.

        This method computes the S-coordinate stretching curves at rho- and
        w-points.

        Parameters
        ----------
        N : int
            Number of vertical levels.
            If not provided, `N` will remain unchanged (i.e., the existing value will not be overwritten).
        theta_s : float
            S-coordinate surface control parameter.
            If not provided, `theta_s` will remain unchanged (i.e., the existing value will not be overwritten).
        theta_b : float
            S-coordinate bottom control parameter.
            If not provided, `theta_b` will remain unchanged (i.e., the existing value will not be overwritten).
        hc : float
            Critical depth (m) used in ROMS vertical coordinate stretching.
            If not provided, `hc` will remain unchanged (i.e., the existing value will not be overwritten).
        verbose: bool, optional
            Indicates whether to print vertical coordinate generation steps with timing. Defaults to False.

        Returns
        -------
        None
            This method modifies the dataset in place by adding vertical coordinate variables.
        """

        N = N or self.N
        theta_s = theta_s or self.theta_s
        theta_b = theta_b or self.theta_b
        hc = hc or self.hc

        if verbose:
            start_time = time.time()
            logging.info(
                f"=== Preparing the vertical coordinate system using N = {N}, theta_s = {theta_s}, theta_b = {theta_b}, hc = {hc} ==="
            )

        ds = self.ds
        # need to drop vertical coordinates because they could cause conflict if N changed
        vars_to_drop = [
            "layer_depth_rho",
            "layer_depth_u",
            "layer_depth_v",
            "interface_depth_rho",
            "interface_depth_u",
            "interface_depth_v",
            "sigma_r",
            "sigma_w",
            "Cs_w",
            "Cs_r",
        ]

        for var in vars_to_drop:
            if var in ds.variables:
                ds = ds.drop_vars(var)

        cs_r, sigma_r = sigma_stretch(theta_s, theta_b, N, "r")
        cs_w, sigma_w = sigma_stretch(theta_s, theta_b, N, "w")

        ds["sigma_r"] = sigma_r.astype(np.float32)
        ds["sigma_r"].attrs[
            "long_name"
        ] = "Fractional vertical stretching coordinate at rho-points"
        ds["sigma_r"].attrs["units"] = "nondimensional"

        ds["Cs_r"] = cs_r.astype(np.float32)
        ds["Cs_r"].attrs["long_name"] = "Vertical stretching function at rho-points"
        ds["Cs_r"].attrs["units"] = "nondimensional"

        ds["sigma_w"] = sigma_w.astype(np.float32)
        ds["sigma_w"].attrs[
            "long_name"
        ] = "Fractional vertical stretching coordinate at w-points"
        ds["sigma_w"].attrs["units"] = "nondimensional"

        ds["Cs_w"] = cs_w.astype(np.float32)
        ds["Cs_w"].attrs["long_name"] = "Vertical stretching function at w-points"
        ds["Cs_w"].attrs["units"] = "nondimensional"

        ds.attrs["theta_s"] = np.float32(theta_s)
        ds.attrs["theta_b"] = np.float32(theta_b)
        ds.attrs["hc"] = np.float32(hc)

        if verbose:
            logging.info(f"Total time: {time.time() - start_time:.3f} seconds")
            logging.info(
                "========================================================================================================"
            )

        object.__setattr__(self, "ds", ds)
        object.__setattr__(self, "theta_s", theta_s)
        object.__setattr__(self, "theta_b", theta_b)
        object.__setattr__(self, "hc", hc)
        object.__setattr__(self, "N", N)

    def _straddle(self) -> None:
        """Check if the Greenwich meridian goes through the domain.

        This method sets the `straddle` attribute to `True` if the Greenwich meridian
        (0° longitude) intersects the domain defined by `lon_rho`. Otherwise, it sets
        the `straddle` attribute to `False`.

        The check is based on whether the longitudinal differences between adjacent
        points exceed 300 degrees, indicating a potential wraparound of longitude.
        """

        if (
            np.abs(self.ds.lon_rho.diff("xi_rho")).max() > 300
            or np.abs(self.ds.lon_rho.diff("eta_rho")).max() > 300
        ):
            object.__setattr__(self, "straddle", True)
        else:
            object.__setattr__(self, "straddle", False)

    def _coarsen(self):
        """Update the grid by adding grid variables that are coarsened versions of the
        original fine-resoluion grid variables. The coarsening is by a factor of two.

        The specific variables being coarsened are:
        - `lon_rho` -> `lon_coarse`: Longitude at rho points.
        - `lat_rho` -> `lat_coarse`: Latitude at rho points.
        - `angle` -> `angle_coarse`: Angle between the xi axis and true east.
        - `mask_rho` -> `mask_coarse`: Land/sea mask at rho points.
        """
        d = {
            "angle": "angle_coarse",
            "mask_rho": "mask_coarse",
            "lat_rho": "lat_coarse",
            "lon_rho": "lon_coarse",
        }

        ds = self.ds

        for fine_var, coarse_var in d.items():
            fine_field = ds[fine_var]
            if self.straddle and fine_var == "lon_rho":
                fine_field = xr.where(fine_field > 180, fine_field - 360, fine_field)

            coarse_field = _f2c(fine_field)
            if fine_var == "lon_rho":
                coarse_field = xr.where(
                    coarse_field < 0, coarse_field + 360, coarse_field
                )
            if coarse_var in ["lon_coarse", "lat_coarse"]:
                ds = ds.assign_coords({coarse_var: coarse_field})
            else:
                ds[coarse_var] = coarse_field

            del fine_field, coarse_field

        ds["mask_coarse"] = xr.where(ds["mask_coarse"] > 0.5, 1, 0).astype(np.int32)

        for fine_var, coarse_var in d.items():
            long_name = ds[fine_var].attrs.get(
                "long_name", ds[fine_var].attrs.get("Long_name", "")
            )
            ds[coarse_var].attrs["long_name"] = f"{long_name} on coarsened grid"
            ds[coarse_var].attrs["units"] = ds[fine_var].attrs["units"]

        object.__setattr__(self, "ds", ds)

    def plot(
        self, bathymetry: bool = True, title: str = None, with_dim_names: bool = False
    ) -> None:
        """Plot the grid.

        Parameters
        ----------
        bathymetry : bool, optional
            Whether or not to plot the bathymetry. Default is True.
        title : str, optional
            The title of the plot. If not provided, it will be set to a default.
        with_dim_names : bool, optional
            Whether or not to plot the dimension names. Default is False.


        Returns
        -------
        None
            This method does not return any value. It generates and displays a plot.
        """

        field = self.ds.h.where(self.ds.mask_rho)
        lat_deg = self.ds.lat_rho
        lon_deg = self.ds.lon_rho
        if self.straddle:
            lon_deg = xr.where(lon_deg > 180, lon_deg - 360, lon_deg)
        field = field.assign_coords({"lon": lon_deg, "lat": lat_deg})

        if bathymetry:
            if title is None:
                title = "ROMS grid and bathymetry"

            vmax = field.max().values
            vmin = field.min().values
            cmap = plt.colormaps.get_cmap("YlGnBu")
            cmap.set_bad(color="gray")
            kwargs = {"vmax": vmax, "vmin": vmin, "cmap": cmap}

            _plot(
                field=field,
                title=title,
                with_dim_names=with_dim_names,
                kwargs=kwargs,
            )
        else:
            if title is None:
                title = "ROMS grid"
            _plot(
                field=field, title=title, with_dim_names=with_dim_names, plot_data=False
            )

    def compute_depth_coordinates(
        self, depth_type: str, locations: list[str] = ["rho", "u", "v"]
    ):
        """Compute and update vertical depth coordinates.

        Calculates vertical depth coordinates (layer or interface) for specified locations (e.g., rho, u, v points)
        and updates them in the dataset (`self.ds`).

        Parameters
        ----------
        depth_type : str
            The type of depth coordinate to compute. Valid options:
            - "layer": Compute layer depth coordinates.
            - "interface": Compute interface depth coordinates.
        locations : list[str], optional
            Locations for which to compute depth coordinates. Default is ["rho", "u", "v"].
            Valid options include:
            - "rho": Depth coordinates at rho points.
            - "u": Depth coordinates at u points.
            - "v": Depth coordinates at v points.

        Updates
        -------
        self.ds : xarray.Dataset
            The dataset (`self.ds`) is updated with the following depth coordinate variables:
            - f"{depth_type}_depth_rho": Depth coordinates at rho points.
            - f"{depth_type}_depth_u": Depth coordinates at u points (if included in `locations`).
            - f"{depth_type}_depth_v": Depth coordinates at v points (if included in `locations`).

        Notes
        -----
        This method uses the `compute_and_update_depth_coordinates` function to perform calculations and updates.
        """
        add_depth_coordinates_to_dataset(self.ds, self.ds, depth_type, locations)

    def plot_vertical_coordinate(
        self,
        s=None,
        eta=None,
        xi=None,
    ) -> None:
        """Plot the layer depth for a given eta-, xi-, or s-slice.

        Parameters
        ----------
        s: int, optional
            The s-index to plot. Default is None.
        eta : int, optional
            The eta-index to plot. Default is None.
        xi : int, optional
            The xi-index to plot. Default is None.
        ax : matplotlib.axes.Axes, optional
            The axes to plot on. If None, a new figure is created. Note that this argument does not work for horizontal plots that display the eta- and xi-dimensions at the same time.

        Returns
        -------
        None
            This method does not return any value. It generates and displays a plot.

        Raises
        ------
        ValueError
            If not exactly one of s, eta, xi is specified.
        """

        title = "Layer depth at rho-points"

        if sum(s is not None for s in [s, eta, xi]) != 1:
            raise ValueError("Exactly one of s, eta, or xi must be specified.")

        h = self.ds["h"]
        lat_deg = self.ds.lat_rho
        lon_deg = self.ds.lon_rho
        if self.straddle:
            lon_deg = xr.where(lon_deg > 180, lon_deg - 360, lon_deg)
        h = h.assign_coords({"lon": lon_deg, "lat": lat_deg})

        # slice the bathymetry as desired
        if eta is not None:
            title = title + f", eta_rho = {h.eta_rho[eta].item()}"
            h = h.isel(eta_rho=eta)
        if xi is not None:
            title = title + f", xi_rho = {h.xi_rho[xi].item()}"
            h = h.isel(xi_rho=xi)

        if eta is None and xi is None:
            layer_depth = compute_depth(0, h, self.hc, self.ds.Cs_r, self.ds.sigma_r)
            title = title + f", s_rho = {layer_depth.s_rho[s].item()}"
            layer_depth = layer_depth.isel(s_rho=s)

            layer_depth.attrs["long_name"] = "Layer depth"
            layer_depth.attrs["units"] = "m"

            vmax = layer_depth.max().values
            vmin = layer_depth.min().values
            cmap = plt.colormaps.get_cmap("YlGnBu")
            cmap.set_bad(color="gray")
            kwargs = {"vmax": vmax, "vmin": vmin, "cmap": cmap}

            _plot(
                field=layer_depth.where(self.ds.mask_rho),
                depth_contours=False,
                title=title,
                kwargs=kwargs,
            )
        else:
            layer_depth = compute_depth(0, h, self.hc, self.ds.Cs_r, self.ds.sigma_r)
            layer_depth.attrs["long_name"] = "Layer depth"
            layer_depth.attrs["units"] = "m"
            field = xr.zeros_like(layer_depth)
            field = field.assign_coords({"layer_depth": layer_depth})

            interface_depth = compute_depth(
                0, h, self.hc, self.ds.Cs_w, self.ds.sigma_w
            )
            cmap = plt.colormaps.get_cmap("YlGnBu")
            cmap.set_bad(color="gray")
            kwargs = {"vmax": 0.0, "vmin": 0.0, "cmap": cmap, "add_colorbar": False}

            _section_plot(
                field=field,
                interface_depth=interface_depth,
                title=title,
                kwargs=kwargs,
            )

    def save(self, filepath: Union[str, Path]) -> None:
        """Save the grid information to a netCDF4 file.

        Parameters
        ----------
        filepath : Union[str, Path]
            The base path or filename where the dataset should be saved.

        Returns
        -------
        List[Path]
            A list of Path objects for the filenames that were saved.
        """

        # Ensure filepath is a Path object
        filepath = Path(filepath)

        # Remove ".nc" suffix if present
        if filepath.suffix == ".nc":
            filepath = filepath.with_suffix("")

        dataset_list = [self.ds.load()]
        output_filenames = [str(filepath)]

        saved_filenames = save_datasets(dataset_list, output_filenames)

        return saved_filenames

    @classmethod
    def from_file(cls, filepath: Union[str, Path], verbose: bool = False) -> "Grid":
        """Create a Grid instance from an existing file.

        Parameters
        ----------
        filepath : Union[str, Path]
            Path to the file containing the grid information.
        verbose: bool, optional
            Indicates whether to print grid generation steps with timing. Defaults to False.

        Returns
        -------
        Grid
            A new instance of Grid populated with data from the file.
        """
        # Load the dataset from the file
        ds = xr.open_dataset(filepath)

        if not all(mask in ds for mask in ["mask_u", "mask_v"]):
            ds = _add_velocity_masks(ds)

        # Create a new Grid instance without calling __init__ and __post_init__
        grid = cls.__new__(cls)

        # Set the dataset for the grid instance
        object.__setattr__(grid, "ds", ds)

        # Check if the Greenwich meridian goes through the domain.
        grid._straddle()

        if not all(coord in grid.ds for coord in ["lat_u", "lon_u", "lat_v", "lon_v"]):
            ds = _add_lat_lon_at_velocity_points(grid.ds, grid.straddle)
            object.__setattr__(grid, "ds", ds)

        # Coarsen the grid if necessary
        if not all(
            var in grid.ds
            for var in [
                "lon_coarse",
                "lat_coarse",
                "angle_coarse",
                "mask_coarse",
            ]
        ):
            grid._coarsen()

        # Move variables to coordinates if necessary
        for var in ["lat_rho", "lon_rho", "lat_coarse", "lon_coarse"]:
            if var not in ds.coords:
                ds = grid.ds.set_coords(var)
                object.__setattr__(grid, "ds", ds)

        # Update vertical coordinate if necessary
        if not all(var in grid.ds for var in ["Cs_r", "Cs_w"]):
            logging.warning("Vertical coordinates (Cs_r, Cs_w) not found in grid file.")
            N = 100
            theta_s = 5.0
            theta_b = 2.0
            hc = 300.0

            grid.update_vertical_coordinate(
                N=N, theta_s=theta_s, theta_b=theta_b, hc=hc, verbose=True
            )
        else:
            # object.__setattr__(grid, "theta_s", ds.attrs["theta_s"].item())
            # object.__setattr__(grid, "theta_b", ds.attrs["theta_b"].item())
            # object.__setattr__(grid, "hc", ds.attrs["hc"].item())
            # object.__setattr__(grid, "N", len(ds.s_rho))
            object.__setattr__(grid, "theta_s", ds["theta_s"])
            object.__setattr__(grid, "theta_b", ds["theta_b"])
            object.__setattr__(grid, "hc", ds["hc"])
            object.__setattr__(grid, "N", len(ds.s_rho))

        # Manually set the remaining attributes by extracting parameters from dataset
        object.__setattr__(grid, "nx", ds.sizes["xi_rho"] - 2)
        object.__setattr__(grid, "ny", ds.sizes["eta_rho"] - 2)
        if "center_lon" in ds.attrs:
            center_lon = ds.attrs["center_lon"]
        elif "tra_lon" in ds:
            center_lon = extract_single_value(ds["tra_lon"])
        else:
            raise ValueError(
                "Missing grid information: 'center_lon' attribute or 'tra_lon' variable "
                "must be present in the dataset."
            )
        object.__setattr__(grid, "center_lon", center_lon)
        if "center_lat" in ds.attrs:
            center_lat = ds.attrs["center_lat"]
        elif "tra_lat" in ds:
            center_lat = extract_single_value(ds["tra_lat"])
        else:
            raise ValueError(
                "Missing grid information: 'center_lat' attribute or 'tra_lat' variable "
                "must be present in the dataset."
            )
        object.__setattr__(grid, "center_lat", center_lat)
        if "rot" in ds.attrs:
            rot = ds.attrs["rot"]
        elif "rotate" in ds:
            rot = extract_single_value(ds["rotate"])
        else:
            raise ValueError(
                "Missing grid information: 'rot' attribute or 'rotate' variable "
                "must be present in the dataset."
            )
        # object.__setattr__(grid, "rot", rot)
        object.__setattr__(grid, "rot", ds["rot"])

        for attr in [
            "size_x",
            "size_y",
            "topography_source",
            "hmin",
        ]:
            if attr in ds.attrs:
                if attr == "topography_source":
                    a = {"name": ds.attrs[attr]}
                else:
                    a = ds.attrs[attr]
            else:
                a = None
            object.__setattr__(grid, attr, a)

        return grid

    def to_yaml(self, filepath: Union[str, Path]) -> None:
        """Export the parameters of the class to a YAML file, including the version of
        roms-tools.

        Parameters
        ----------
        filepath : Union[str, Path]
            The path to the YAML file where the parameters will be saved.
        """

        filepath = Path(filepath)

        data = asdict(self)
        data.pop("ds", None)
        data.pop("straddle", None)
        data.pop("verbose", None)

        # Include the version of roms-tools
        try:
            roms_tools_version = importlib.metadata.version("roms-tools")
        except importlib.metadata.PackageNotFoundError:
            roms_tools_version = "unknown"

        # Create header
        header = f"---\nroms_tools_version: {roms_tools_version}\n---\n"

        # Use the class name as the top-level key
        yaml_data = {self.__class__.__name__: data}

        with filepath.open("w") as file:
            # Write header
            file.write(header)
            # Write YAML data
            yaml.dump(yaml_data, file, default_flow_style=False, sort_keys=False)

    @classmethod
    def from_yaml(
        cls,
        filepath: Union[str, Path],
        section_name: str = "Grid",
        verbose: bool = False,
    ) -> "Grid":
        """Create an instance of the class from a YAML file.

        Parameters
        ----------
        filepath : Union[str, Path]
            The path to the YAML file from which the parameters will be read.
        section_name : str, optional
            The name of the YAML section containing the grid configuration. Defaults to "Grid".
        verbose : bool, optional
            Indicates whether to print grid generation steps with timing. Defaults to False.

        Returns
        -------
        Grid
            An instance of the Grid class initialized with the parameters from the YAML file.

        Raises
        ------
        ValueError
            If the ROMS-Tools version is not found in the YAML file or if the specified section
            does not exist in the file.

        Warnings
        --------
        Issues a warning if the ROMS-Tools version in the YAML header does not match the
        currently installed version.
        """

        filepath = Path(filepath)
        # Read the entire file content
        with filepath.open("r") as file:
            file_content = file.read()

        # Split the content into YAML documents
        documents = list(yaml.safe_load_all(file_content))

        header_data = None
        grid_data = None

        # Iterate over documents to find the header and grid configuration
        for doc in documents:
            if doc is None:
                continue
            if "roms_tools_version" in doc:
                header_data = doc
            elif section_name in doc:
                grid_data = doc[section_name]

        if header_data is None:
            raise ValueError("Version of ROMS-Tools not found in the YAML file.")
        else:
            # Check the roms_tools_version
            roms_tools_version_header = header_data.get("roms_tools_version")
            # Get current version of roms-tools
            try:
                roms_tools_version_current = importlib.metadata.version("roms-tools")
            except importlib.metadata.PackageNotFoundError:
                roms_tools_version_current = "unknown"

            if roms_tools_version_header != roms_tools_version_current:
                logging.warning(
                    f"Current roms-tools version ({roms_tools_version_current}) does not match the version in the YAML header ({roms_tools_version_header}).",
                )

        if grid_data is None:
            raise ValueError("No Grid configuration found in the YAML file.")
        return cls(**grid_data, verbose=verbose)

    def __repr__(self) -> str:
        """Return a string representation of the object with non-None attributes,
        excluding 'ds'."""
        cls = self.__class__
        cls_name = cls.__name__
        # Filter attributes to exclude 'ds' and those with None values
        attr_dict = {
            k: v for k, v in self.__dict__.items() if k != "ds" and v is not None
        }
        attr_str = ", ".join(f"{k}={v!r}" for k, v in attr_dict.items())
        return f"{cls_name}({attr_str})"

    def _create_horizontal_grid(self) -> xr.Dataset():
        """Create the horizontal grid based on a Mercator projection and store it in the
        'ds' attribute.

        Parameters
        ----------
        None

        Returns
        -------
        xr.Dataset
            The created horizontal grid dataset, including coordinates, grid metrics, angles, and metadata.

        Notes
        -----
        - Longitude values are adjusted to fall within the range [0, 360].
        - Grid rotation and translation are applied based on the specified parameters.
        """
        if self.verbose:
            start_time = time.time()
            logging.info("=== Creating the horizontal grid ===")

        self._raise_if_domain_size_too_large()

        coords = self._make_initial_lon_lat_ds()

        # rotate coordinate system
        coords = _rotate(coords, self.rot)

        # translate coordinate system
        coords = _translate(coords, self.center_lat, self.center_lon)

        # compute 1/dx and 1/dy
        coords["pm"], coords["pn"] = _compute_coordinate_metrics(coords)

        # compute angle of local grid positive x-axis relative to east
        coords["angle"] = _compute_angle(coords)

        # make sure lons are in [0, 360] range
        for lon in ["lon", "lonu", "lonv", "lonq"]:
            coords[lon][coords[lon] < 0] = coords[lon][coords[lon] < 0] + 2 * np.pi

        ds = self._create_grid_ds(coords)

        ds = self._add_global_metadata(ds)

        if self.verbose:
            logging.info(f"Total time: {time.time() - start_time:.3f} seconds")
            logging.info(
                "========================================================================================================"
            )

        object.__setattr__(self, "ds", ds)

    def _add_global_metadata(self, ds):
        """Add global metadata and attributes to the dataset.

        Parameters
        ----------
        ds : xr.Dataset
            Dataset to which global metadata and attributes will be added.

        Returns
        -------
        xr.Dataset
            The dataset with added global metadata, including grid type, tool version,
            grid dimensions, center coordinates, and rotation.

        Notes
        -----
        - The "spherical" attribute indicates the grid type and is set to "T" (spherical).
        - The ROMS-Tools version is included as "roms_tools_version". If unavailable, it defaults to "unknown".
        """
        ds["spherical"] = xr.DataArray(np.array("T", dtype="S1"))
        ds["spherical"].attrs["Long_name"] = "Grid type logical switch"
        ds["spherical"].attrs["option_T"] = "spherical"

        ds.attrs["title"] = "ROMS grid created by ROMS-Tools"

        # Include the version of roms-tools
        try:
            roms_tools_version = importlib.metadata.version("roms-tools")
        except importlib.metadata.PackageNotFoundError:
            roms_tools_version = "unknown"

        ds.attrs["roms_tools_version"] = roms_tools_version
        ds.attrs["size_x"] = self.size_x
        ds.attrs["size_y"] = self.size_y
        ds.attrs["center_lon"] = self.center_lon
        ds.attrs["center_lat"] = self.center_lat
        ds.attrs["rot"] = self.rot

        return ds

    def _raise_if_domain_size_too_large(self):
        """Raise a ValueError if the domain size exceeds the allowable threshold.

        Checks if either the x or y domain size exceeds 20,000 km and raises an error
        with appropriate details if the threshold is surpassed.
        """
        threshold = 20000
        if self.size_x > threshold or self.size_y > threshold:
            raise ValueError(
                f"Domain size exceeds the allowable limit of {threshold} km. "
                f"Received dimensions: size_x = {self.size_x} km, size_y = {self.size_y} km. "
                "Please reduce the domain size to meet the threshold."
            )

    def _make_initial_lon_lat_ds(self):
        """Generate initial longitude and latitude arrays with Mercator projection
        around the equator.

        Returns
        -------
        dict
            A dictionary containing the following arrays:
            - lon, lat: 2D arrays of longitudes and latitudes at cell centers.
            - lonu, latu: 2D arrays of longitudes and latitudes at u-points.
            - lonv, latv: 2D arrays of longitudes and latitudes at v-points.
            - lonq, latq: 2D arrays of longitudes and latitudes at cell corners.
        """

        r_earth = 6371315.0

        # initially define the domain to be longer in x-direction (dimension "length")
        # than in y-direction (dimension "width") to keep grid distortion minimal
        if self.size_y > self.size_x:
            domain_length, domain_width = self.size_y * 1e3, self.size_x * 1e3  # in m
            nl, nw = self.ny, self.nx
        else:
            domain_length, domain_width = self.size_x * 1e3, self.size_y * 1e3  # in m
            nl, nw = self.nx, self.ny

        domain_length_in_degrees = domain_length / r_earth
        domain_width_in_degrees = domain_width / r_earth

        # Generate 1D longitude arrays at cell centers and corners
        lon_array_1d_in_degrees = domain_length_in_degrees * (
            np.arange(-0.5, nl + 1.5) / nl - 0.5
        )
        lonq_array_1d_in_degrees_q = domain_length_in_degrees * (
            np.arange(-1, nl + 2) / nl - 0.5
        )

        # Mercator projection for latitude
        y1 = np.log(np.tan(np.pi / 4 - domain_width_in_degrees / 4))
        y2 = np.log(np.tan(np.pi / 4 + domain_width_in_degrees / 4))

        # Generate 1D latitude arrays with inverse Mercator projection
        lat_array_1d_in_degrees = np.arctan(
            np.sinh((y2 - y1) * (np.arange(-0.5, nw + 1.5) / nw) + y1)
        )
        latq_array_1d_in_degrees = np.arctan(
            np.sinh((y2 - y1) * (np.arange(-1, nw + 2) / nw) + y1)
        )

        # 2D grids for cell centers and corners
        lon, lat = np.meshgrid(lon_array_1d_in_degrees, lat_array_1d_in_degrees)
        lonq, latq = np.meshgrid(lonq_array_1d_in_degrees_q, latq_array_1d_in_degrees)

        if self.size_y > self.size_x:
            # Rotate grid by 90 degrees because until here the grid has been defined
            # to be longer in x-direction than in y-direction

            lon, lat = _rot_sphere(lon, lat, 90)
            lonq, latq = _rot_sphere(lonq, latq, 90)

            lon = np.transpose(np.flip(lon, 0))
            lat = np.transpose(np.flip(lat, 0))
            lonq = np.transpose(np.flip(lonq, 0))
            latq = np.transpose(np.flip(latq, 0))

        # Inference for u- and v-point coordinates
        lonu = 0.5 * (lon[:, :-1] + lon[:, 1:])
        latu = 0.5 * (lat[:, :-1] + lat[:, 1:])
        lonv = 0.5 * (lon[:-1, :] + lon[1:, :])
        latv = 0.5 * (lat[:-1, :] + lat[1:, :])

        coords = {
            "lon": lon,
            "lat": lat,
            "lonu": lonu,
            "latu": latu,
            "lonv": lonv,
            "latv": latv,
            "lonq": lonq,
            "latq": latq,
        }

        return coords

    def _create_grid_ds(self, coords):
        """Create an xarray Dataset with grid coordinates and metrics.

        Parameters
        ----------
        coords : dict
            Dictionary containing:
            - lon, lat, lonu, latu, lonv, latv : 1d arrays of coordinates (degrees)
            - angle : 2d array (radians)
            - pm, pn : 2d arrays (meter^-1)

        Returns
        -------
        xarray.Dataset
            Dataset with variables: lon_rho, lat_rho, lon_u, lat_u, lon_v, lat_v,
            angle, f (Coriolis parameter), pm, pn.
        """

        ds = xr.Dataset()

        lon_rho = xr.Variable(
            data=coords["lon"] * 180 / np.pi,
            dims=["eta_rho", "xi_rho"],
            attrs={"long_name": "longitude of rho-points", "units": "degrees East"},
        )
        lat_rho = xr.Variable(
            data=coords["lat"] * 180 / np.pi,
            dims=["eta_rho", "xi_rho"],
            attrs={"long_name": "latitude of rho-points", "units": "degrees North"},
        )
        lon_u = xr.Variable(
            data=coords["lonu"] * 180 / np.pi,
            dims=["eta_rho", "xi_u"],
            attrs={"long_name": "longitude of u-points", "units": "degrees East"},
        )
        lat_u = xr.Variable(
            data=coords["latu"] * 180 / np.pi,
            dims=["eta_rho", "xi_u"],
            attrs={"long_name": "latitude of u-points", "units": "degrees North"},
        )
        lon_v = xr.Variable(
            data=coords["lonv"] * 180 / np.pi,
            dims=["eta_v", "xi_rho"],
            attrs={"long_name": "longitude of v-points", "units": "degrees East"},
        )
        lat_v = xr.Variable(
            data=coords["latv"] * 180 / np.pi,
            dims=["eta_v", "xi_rho"],
            attrs={"long_name": "latitude of v-points", "units": "degrees North"},
        )
        # lon_q = xr.Variable(
        #    data=coords["lonq"] * 180 / np.pi,
        #    dims=["eta_psi", "xi_psi"],
        #    attrs={"long_name": "longitude of psi-points", "units": "degrees East"},
        # )
        # lat_q = xr.Variable(
        #    data=coords["latq"] * 180 / np.pi,
        #    dims=["eta_psi", "xi_psi"],
        #    attrs={"long_name": "latitude of psi-points", "units": "degrees North"},
        # )

        ds = ds.assign_coords(
            {
                "lat_rho": lat_rho,
                "lon_rho": lon_rho,
                "lat_u": lat_u,
                "lon_u": lon_u,
                "lat_v": lat_v,
                "lon_v": lon_v,
                # "lat_psi": lat_q,
                # "lon_psi": lon_q,
            }
        )

        ds["angle"] = xr.Variable(
            data=coords["angle"],
            dims=["eta_rho", "xi_rho"],
            attrs={"long_name": "Angle between xi axis and east", "units": "radians"},
        )

        # Coriolis frequency
        f0 = 4 * np.pi * np.sin(coords["lat"]) / (24 * 3600)

        ds["f"] = xr.Variable(
            data=f0,
            dims=["eta_rho", "xi_rho"],
            attrs={
                "long_name": "Coriolis parameter at rho-points",
                "units": "second-1",
            },
        )

        ds["pm"] = xr.Variable(
            data=coords["pm"],
            dims=["eta_rho", "xi_rho"],
            attrs={
                "long_name": "Curvilinear coordinate metric in xi-direction",
                "units": "meter-1",
            },
        )
        ds["pn"] = xr.Variable(
            data=coords["pn"],
            dims=["eta_rho", "xi_rho"],
            attrs={
                "long_name": "Curvilinear coordinate metric in eta-direction",
                "units": "meter-1",
            },
        )

        return ds


def _rotate(coords, rot):
    """Rotate grid counterclockwise relative to surface of Earth by rot degrees."""

    (coords["lon"], coords["lat"]) = _rot_sphere(coords["lon"], coords["lat"], rot)
    (coords["lonu"], coords["latu"]) = _rot_sphere(coords["lonu"], coords["latu"], rot)
    (coords["lonv"], coords["latv"]) = _rot_sphere(coords["lonv"], coords["latv"], rot)
    (coords["lonq"], coords["latq"]) = _rot_sphere(coords["lonq"], coords["latq"], rot)

    return coords


def _translate(coords, tra_lat, tra_lon):
    """Translate grid so that the centre lies at the position (tra_lat, tra_lon)"""

    (lon, lat) = _tra_sphere(coords["lon"], coords["lat"], tra_lat)
    (lonu, latu) = _tra_sphere(coords["lonu"], coords["latu"], tra_lat)
    (lonv, latv) = _tra_sphere(coords["lonv"], coords["latv"], tra_lat)
    (lonq, latq) = _tra_sphere(coords["lonq"], coords["latq"], tra_lat)

    lon = lon + tra_lon * np.pi / 180
    lonu = lonu + tra_lon * np.pi / 180
    lonv = lonv + tra_lon * np.pi / 180
    lonq = lonq + tra_lon * np.pi / 180

    lon[lon < -np.pi] = lon[lon < -np.pi] + 2 * np.pi
    lonu[lonu < -np.pi] = lonu[lonu < -np.pi] + 2 * np.pi
    lonv[lonv < -np.pi] = lonv[lonv < -np.pi] + 2 * np.pi
    lonq[lonq < -np.pi] = lonq[lonq < -np.pi] + 2 * np.pi

    coords = {
        "lon": lon,
        "lat": lat,
        "lonu": lonu,
        "latu": latu,
        "lonv": lonv,
        "latv": latv,
        "lonq": lonq,
        "latq": latq,
    }

    return coords


def _rot_sphere(lon, lat, rot):
    """Rotate longitude and latitude coordinates on a sphere.

    Parameters
    ----------
    lon : ndarray
        2D array of longitudes in radians.
    lat : ndarray
        2D array of latitudes in radians.
    rot : float
        Rotation angle in degrees.

    Returns
    -------
    tuple
        Rotated longitude and latitude arrays (lon, lat) in radians.
    """
    # Convert rotation angle from degrees to radians
    rot = rot * np.pi / 180

    # Convert spherical coordinates to Cartesian coordinates (x, y, z)
    x1 = np.sin(lon) * np.cos(lat)
    y1 = np.cos(lon) * np.cos(lat)
    z1 = np.sin(lat)

    # Calculate the radial distance in the x-z plane
    rp1 = np.sqrt(x1**2 + z1**2)

    # Compute azimuthal angle
    ap1 = np.arctan2(np.abs(z1), np.abs(x1))
    ap1[x1 < 0] = np.pi - ap1[x1 < 0]
    ap1[z1 < 0] = -ap1[z1 < 0]

    # Apply rotation to the azimuthal angle
    ap2 = ap1 + rot
    x2 = rp1 * np.cos(ap2)
    y2 = y1
    z2 = rp1 * np.sin(ap2)

    # Recompute longitude and latitude
    lon_rot = np.arctan2(np.abs(x2), np.abs(y2))
    lon_rot[y2 < 0] = np.pi - lon_rot[y2 < 0]
    lon_rot[x2 < 0] = -lon_rot[x2 < 0]

    pr2 = np.sqrt(x2**2 + y2**2)
    lat_rot = np.arctan2(np.abs(z2), pr2)
    lat_rot[z2 < 0] = -lat_rot[z2 < 0]

    return lon_rot, lat_rot


def _tra_sphere(lon, lat, tra):
    """Translate longitude and latitude coordinates on a sphere in the latitude
    direction.

    Parameters
    ----------
    lon : ndarray
        2D array of longitudes in radians.
    lat : ndarray
        2D array of latitudes in radians.
    tra : float
        Translation angle in degrees.

    Returns
    -------
    tuple
        Translated longitude and latitude arrays (lon, lat) in radians.
    """

    # Convert translation angle from degrees to radians
    tra = tra * np.pi / 180

    # Convert spherical coordinates to Cartesian coordinates (x, y, z)
    x1 = np.sin(lon) * np.cos(lat)
    y1 = np.cos(lon) * np.cos(lat)
    z1 = np.sin(lat)

    # Radial distance in the y-z plane
    rp1 = np.sqrt(y1**2 + z1**2)

    # Compute azimuthal angle in the y-z plane
    ap1 = np.arctan2(np.abs(z1), np.abs(y1))
    ap1[y1 < 0] = np.pi - ap1[y1 < 0]
    ap1[z1 < 0] = -ap1[z1 < 0]

    # Apply translation in the azimuthal angle
    ap2 = ap1 + tra
    y2 = rp1 * np.cos(ap2)
    z2 = rp1 * np.sin(ap2)

    # Convert back to spherical coordinates
    lon_rot = np.arctan2(np.abs(x1), np.abs(y2))
    lon_rot[y2 < 0] = np.pi - lon_rot[y2 < 0]
    lon_rot[x1 < 0] = -lon_rot[x1 < 0]

    pr2 = np.sqrt(x1**2 + y2**2)
    lat_rot = np.arctan2(np.abs(z2), pr2)
    lat_rot[z2 < 0] = -lat_rot[z2 < 0]

    return lon_rot, lat_rot


def _compute_coordinate_metrics(coords):
    """Compute the reciprocal of grid spacing (`pn` and `pm`) in the latitude and
    longitude directions.

    Parameters
    ----------
    coords : dict
        A dictionary containing coordinate arrays 'lonu', 'latu', 'lonv', and 'latv' for the u- and v-velocity points.

    Returns
    -------
    pn : ndarray
        The metric for the latitude direction (1/dy).

    pm : ndarray
        The metric for the longitude direction (1/dx).

    Notes
    -----
    Boundary values of `pn` and `pm` are copied from adjacent interior values.
    """

    # pm = 1/dx
    pmu = gc_dist(
        coords["lonu"][:, :-1],
        coords["latu"][:, :-1],
        coords["lonu"][:, 1:],
        coords["latu"][:, 1:],
        input_in_degrees=False,
    )
    pm = np.zeros_like(coords["lon"])
    pm[:, 1:-1] = pmu
    # Handle boundary conditions
    pm[:, 0] = pm[:, 1]
    pm[:, -1] = pm[:, -2]
    pm = 1 / pm

    # pn = 1/dy
    pnv = gc_dist(
        coords["lonv"][:-1, :],
        coords["latv"][:-1, :],
        coords["lonv"][1:, :],
        coords["latv"][1:, :],
        input_in_degrees=False,
    )
    pn = np.zeros_like(coords["lon"])
    pn[1:-1, :] = pnv
    # Handle boundary conditions
    pn[0, :] = pn[1, :]
    pn[-1, :] = pn[-2, :]
    pn = 1 / pn

    return pn, pm


def _compute_angle(coords):
    """Compute angles of the local grid's positive x-axis relative to east.

    The angle is computed for each grid cell using the latitude and longitude
    differences between neighboring grid points. The result is wrapped to
    the range [-π, π] and adjusted based on longitude and latitude conditions.

    Parameters
    ----------
    coords : dict
        A dictionary containing 'latu' (latitudes) and 'lonu' (longitudes) arrays.

    Returns
    -------
    ang : ndarray
        An array of angles (in radians) of the local grid's positive x-axis
        relative to east for each grid point.
    """

    # Compute differences in latitudes and longitudes
    dellat = coords["latu"][:, 1:] - coords["latu"][:, :-1]
    dellon = coords["lonu"][:, 1:] - coords["lonu"][:, :-1]

    # Normalize longitude differences to the range [-π, π]
    dellon = (dellon + np.pi) % (2 * np.pi) - np.pi
    dellon *= np.cos(0.5 * (coords["latu"][:, 1:] + coords["latu"][:, :-1]))

    # Compute the angle in radians
    ang_s = np.arctan2(dellat, dellon)

    # Adjust angles based on longitude and latitude conditions
    ang_s[(dellon < 0) & (dellat < 0)] -= np.pi
    ang_s[(dellon < 0) & (dellat >= 0)] += np.pi
    ang_s = np.mod(ang_s + np.pi, 2 * np.pi) - np.pi  # Ensure angles are in [-π, π]

    # Create output array and set angles
    ang = np.zeros_like(coords["lon"])
    ang[:, 1:-1] = ang_s
    ang[:, 0] = ang[:, 1]  # Set first column to the second column
    ang[:, -1] = ang[:, -2]  # Set last column to the second-to-last column

    return ang


def _f2c(f):
    """Coarsen input xarray DataArray f in both x- and y-direction.

    Parameters
    ----------
    f : xarray.DataArray
        Input DataArray with dimensions (nxp, nyp).

    Returns
    -------
    fc : xarray.DataArray
        Output DataArray with modified dimensions and values.
    """

    fc = _f2c_xdir(f)
    fc = fc.transpose()
    fc = _f2c_xdir(fc)
    fc = fc.transpose()
    fc = fc.rename({"eta_rho": "eta_coarse", "xi_rho": "xi_coarse"})

    return fc


def _f2c_xdir(f):
    """Coarsen input xarray DataArray f in x-direction.

    Parameters
    ----------
    f : xarray.DataArray
        Input DataArray with dimensions (nxp, nyp).

    Returns
    -------
    fc : xarray.DataArray
        Output DataArray with modified dimensions and values.
    """
    nxp, nyp = f.shape
    nxcp = (nxp - 2) // 2 + 2

    fc = xr.DataArray(np.zeros((nxcp, nyp)), dims=f.dims)

    # Calculate the interior values
    fc[1:-1, :] = 0.5 * (f[1:-2:2, :] + f[2:-1:2, :])

    # Calculate the first row
    fc[0, :] = f[0, :] + 0.5 * (f[0, :] - f[1, :])

    # Calculate the last row
    fc[-1, :] = f[-1, :] + 0.5 * (f[-1, :] - f[-2, :])

    return fc


def _add_lat_lon_at_velocity_points(ds, straddle):
    """Adds latitude and longitude coordinates at velocity points (u and v points) to
    the dataset. This function computes approximate latitude and longitude values at u
    and v velocity points based on the rho points (cell centers). If the grid straddles
    the Greenwich meridian, it adjusts the longitudes to avoid jumps from 360 to 0
    degrees. The computed coordinates are added to the dataset as new variables with
    appropriate metadata.

    Parameters
    ----------
    ds : xarray.Dataset
        The input dataset containing rho point coordinates ("lat_rho", "lon_rho").
    straddle : bool
        Indicates whether the grid straddles the Greenwich meridian. If True, longitudes are adjusted
        to avoid discontinuities.
    Returns
    -------
    ds : xarray.Dataset
        The dataset with added coordinates for u and v points ("lat_u", "lon_u", "lat_v", "lon_v").
    Notes
    -----
    This function only computes approximate latitude and longitude values. It should only be used if
    more accurate values are not available from grid generation.
    """
    if straddle:
        # avoid jump from 360 to 0 in interpolation
        lon_rho = xr.where(ds["lon_rho"] > 180, ds["lon_rho"] - 360, ds["lon_rho"])
    else:
        lon_rho = ds["lon_rho"]
    lat_rho = ds["lat_rho"]

    lat_u = interpolate_from_rho_to_u(lat_rho)
    lon_u = interpolate_from_rho_to_u(lon_rho)
    lat_v = interpolate_from_rho_to_v(lat_rho)
    lon_v = interpolate_from_rho_to_v(lon_rho)

    if straddle:
        # convert back to range [0, 360]
        lon_u = xr.where(lon_u < 0, lon_u + 360, lon_u)
        lon_v = xr.where(lon_v < 0, lon_v + 360, lon_v)

    lat_u.attrs = {"long_name": "latitude of u-points", "units": "degrees North"}
    lon_u.attrs = {"long_name": "longitude of u-points", "units": "degrees East"}
    lat_v.attrs = {"long_name": "latitude of v-points", "units": "degrees North"}
    lon_v.attrs = {"long_name": "longitude of v-points", "units": "degrees East"}

    ds = ds.assign_coords(
        {
            "lat_u": lat_u,
            "lon_u": lon_u,
            "lat_v": lat_v,
            "lon_v": lon_v,
        }
    )

    return ds
