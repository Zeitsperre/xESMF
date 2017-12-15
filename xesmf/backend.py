'''
Backend for xESMF. This module wraps ESMPy's complicated API and can create
ESMF Grid and Regrid objects only using basic numpy arrays.

General idea:
1) Only use pure numpy array in this low-level backend. xarray should only be
used in higher-level APIs which interface with this low-level backend.

2) Use simple, procedural programming here. Because ESMPy Classes are
complicated enough, building new Classes will make debugging very difficult.

3) Add some basic error checking in this wrapper level.
ESMPy is hard to debug because the program often dies in the Fortran level.
So it would be helpful to catch some common mistakes in Python level.

Jiawei Zhuang (12/14/2017)
'''

import numpy as np
import ESMF
import warnings
import os


def warn_f_contiguous(a):
    '''
    Give a warning if input array if not Fortran-ordered.

    ESMPy expects Fortran-ordered array. Passing C-ordered array will slow down
    performance due to memory rearrangement.

    Parameters
    ----------
    a: numpy array
    '''
    if not a.flags['F_CONTIGUOUS']:
        warnings.warn("Input array is not F_CONTIGUOUS. "
                      "Will affect performance.")


def esmf_grid(lon, lat):
    '''
    Create an ESMF.Grid object, to contrust ESMF.Field() and ESMF.Regrid()

    Parameters
    ----------
    lon, lat : 2D numpy array
         Longitute/Latitude of cell centers

         Recommend Fortran-ordering to match ESMPy internal.
         Shape should be (Nlon, Nlat), or (Nx, Ny) for general rectilinear grid

    Returns
    -------
    grid: ESMF.Grid object

    '''

    # ESMPy expects Fortran-ordered array.
    # Passing C-ordered array will slow down performance.
    for a in [lon, lat]:
        warn_f_contiguous(a)

    # ESMF.Grid can actually take 3D array (lon, lat, radius),
    # but regridding only works for 2D array
    assert lon.ndim == 2, "Input grid must be 2D array"
    assert lon.shape == lat.shape, "lon and lat must have same shape"

    staggerloc = ESMF.StaggerLoc.CENTER  # actually just integer 0

    # ESMPy documentation claims that if staggerloc and coord_sys are None,
    # they will be set to default values (CENTER and SPH_DEG).
    # However, they actually need to be set explicitly,
    # otherwise grid._coord_sys and grid._staggerloc will still be None.
    grid = ESMF.Grid(np.array(lon.shape), staggerloc=staggerloc,
                     coord_sys=ESMF.CoordSys.SPH_DEG)

    # The grid object points to the underlying Fortran arrays in ESMF.
    # To modify lat/lon coordinates, need to get pointers to them
    lon_pointer = grid.get_coords(coord_dim=0, staggerloc=staggerloc)
    lat_pointer = grid.get_coords(coord_dim=1, staggerloc=staggerloc)

    # Use [...] to avoid overwritting the object. Only change array values.
    lon_pointer[...] = lon
    lat_pointer[...] = lat

    return grid


def add_corner(grid, lon_b, lat_b):
    '''
    Add corner information to ESMF.Grid for conservative regridding.

    Not needed for other methods like bilinear or nearest neighbour.

    Parameters
    ----------
    grid: ESMF.Grid object, generated by esmf_grid()
        Will be modified in-place

    lon_b, lat_b : 2D numpy array
        Longitute/Latitude of cell corner

        Recommend Fortran-ordering to match ESMPy internal.
        Shape should be (Nlon+1, Nlat+1), or (Nx+1, Ny+1)
    '''

    # codes here are almost the same as esmf_grid(),
    # except for the "staggerloc" keyword
    staggerloc = ESMF.StaggerLoc.CORNER  # actually just integer 3

    for a in [lon_b, lat_b]:
        warn_f_contiguous(a)

    assert lon_b.ndim == 2, "Input grid must be 2D array"
    assert lon_b.shape == lat_b.shape, "lon_b and lat_b must have same shape"
    assert np.array_equal(lon_b.shape, grid.max_index+1), (
           "lon_b should be size (Nx+1, Ny+1)")

    grid.add_coords(staggerloc=staggerloc)

    lon_b_pointer = grid.get_coords(coord_dim=0, staggerloc=staggerloc)
    lat_b_pointer = grid.get_coords(coord_dim=1, staggerloc=staggerloc)

    lon_b_pointer[...] = lon_b
    lat_b_pointer[...] = lat_b


def esmf_regrid_build(sourcegrid, destgrid, method,
                      filename=None, extra_dims=None):
    '''
    Create an ESMF.Regrid object, containing regridding weights.

    Parameters
    ----------
    sourcegrid, destgrid: ESMF.Grid object
        Source and destination grids.

        Users should create them by esmf_grid() and optionally add_corner(),
        instead of ESMPy's original API.

    method: str
        Regridding method. Options are
        - 'bilinear'
        - 'conservative', **need grid corner information**
        - 'patch'
        - 'nearest_s2d'
        - 'nearest_d2s'

    filename: str, optional
        Offline weight file. **Require ESMPy 7.1.0.dev38 or newer.**

        With the weights available, we can use Scipy's sparse matrix
        mulplication to apply weights, which is faster and more Pythonic
        than ESMPy's online regridding.

    extra_dims: a list of integers, optional
        Extra dimensions (e.g. time or levels) in the data field

        This does NOT affect offline weight file, only affects online regrid.

        Extra dimensions will be stacked to the fastest-changing dimensions,
        i.e. following Fortran-like instead of C-like conventions.
        For example, if extra_dims=[Nlev, Ntime], then the data field dimension
        will be [Nlon, Nlat, Nlev, Ntime]

    Returns
    -------
    grid: ESMF.Grid object

    '''

    # use shorter, clearer names for options in ESMF.RegridMethod
    method_dict = {'bilinear': ESMF.RegridMethod.BILINEAR,
                   'conservative': ESMF.RegridMethod.CONSERVE,
                   'patch': ESMF.RegridMethod.PATCH,
                   'nearest_s2d': ESMF.RegridMethod.NEAREST_STOD,
                   'nearest_d2s': ESMF.RegridMethod.NEAREST_DTOS
                   }
    try:
        esmf_regrid_method = method_dict[method]
    except:
        raise ValueError('method should be chosen from '
                         '{}'.format(list(method_dict.keys())))

    # conservative regridding needs cell corner information
    if method == 'conservative':
        if not sourcegrid.has_corners:
            raise ValueError('source grid has no corner information. '
                             'cannot use conservative regridding.')
        if not destgrid.has_corners:
            raise ValueError('destination grid has no corner information. '
                             'cannot use conservative regridding.')

    # ESMF.Regrid requires Field (Grid+data) as input, not just Grid.
    # Extra dimensions are specified when constructing the Field objects,
    # not when constructing the Regrid object later on.
    sourcefield = ESMF.Field(sourcegrid, ndbounds=extra_dims)
    destfield = ESMF.Field(destgrid, ndbounds=extra_dims)

    # ESMPy will throw an incomprehensive error if the weight file
    # already exists. Better to catch it here!
    if filename is not None:
        assert not os.path.exists(filename), (
            'Weight file already exists! Please remove it or use a new name.')

    # Calculate regridding weights.
    # Must set unmapped_action to IGNORE, otherwise the function will fail,
    # if the destination grid is larger than the source grid.
    regrid = ESMF.Regrid(sourcefield, destfield, filename=filename,
                         regrid_method=esmf_regrid_method,
                         unmapped_action=ESMF.UnmappedAction.IGNORE)

    return regrid


def esmf_regrid_apply(regrid, indata):
    '''
    Apply existing regridding weights to the data field,
    using ESMPy's built-in functionality.

    Users are recommended to use Scipy backend instead of this.
    However, this is useful for benchmarking Scipy's result.

    Parameters
    ----------
    regrid: ESMF.Regrid object
        Contains the mapping from the source grid to the destination grid.

        Users should create them by esmf_regrid_build(),
        instead of ESMPy's original API.

    indata: numpy array of shape [Nlon, Nlat, N1, N2, ...]
        Extra dimensions [N1, N2, ...] are specified in esmf_regrid_build()

        Recommend Fortran-ordering to match ESMPy internal.

    Returns
    -------
    outdata: numpy array of shape [Nlon_out, Nlat_out, N1, N2, ...]

    '''

    # Passing C-ordered input data will be terribly slow,
    # since indata is often quite large and re-ordering memory is expensive.
    warn_f_contiguous(indata)

    # Get the pointers to source and destination fields.
    # Because the regrid object points to its underlying field&grid,
    # we can just pass regrid from ESMF_regrid_build() to ESMF_regrid_apply(),
    # without having to pass all the field&grid objects.
    sourcefield = regrid.srcfield
    destfield = regrid.dstfield

    # pass numpy array to the underlying Fortran array
    sourcefield.data[...] = indata

    # apply regridding weights
    destfield = regrid(sourcefield, destfield)

    return destfield.data


def esmf_regrid_finalize(regrid):
    '''
    Free the underlying Fortran array to avoid memory leak.

    After calling destroy() on regrid or its fields, we cannot use the
    regrid method anymore, but the input and output data still exist.

    Parameters
    ----------
    regrid: ESMF.Regrid object

    '''

    regrid.srcfield.destroy()
    regrid.dstfield.destroy()
    regrid.destroy()

    # double check
    assert regrid.srcfield.finalized
    assert regrid.dstfield.finalized
    assert regrid.finalized
