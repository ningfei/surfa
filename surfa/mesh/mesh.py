import os
import numpy as np
from copy import deepcopy
from scipy.spatial import cKDTree
from scipy.sparse import coo_matrix

from surfa.core.array import check_array
from surfa.core.array import normalize
from surfa.mesh.cache import cached_mesh_property
from surfa.mesh.overlay import cast_overlay
from surfa.mesh.sphere import mesh_is_sphere
from surfa.mesh.ray import RayIntersectionQuery
from surfa.mesh.intersection import triangle_intersections
from surfa.transform import ImageGeometry
from surfa.transform import image_geometry_equal
from surfa.transform import cast_image_geometry
from surfa.transform import cast_space


class Mesh:

    def __init__(self, vertices, faces=None, space='surf', geometry=None, metadata=None):
        """
        Triangular mesh topology represented by arrays of vertices and faces.

        Properties of the mesh graph are automatically recomputed when the vertex and face
        data is updated.

        Parameters
        ----------
        vertices : (V, 3) float
            Mesh vertex locations
        faces : (F, 3) int
            Triangular faces indices.
        space : Space
            Coordinate space of the point data. Defaults to the 'surface' coordinate system.
        geometry : ImageGeometry
            Geometry mapping the point data to world and image coordinates.
        metadata : dict
            Dictionary containing arbitrary array metadata.
        """
        self.vertices = vertices
        self.faces = faces
        self.space = space
        self.geom = geometry

        # there are many properties of the mesh that will need to be recomputed every
        # time the mesh geometry (i.e. vertices and faces) is updated, so to deal with
        # this, we build an internal cache to recompute certain properties only when
        # completely necessary (see `surfa/mesh/cache.py` for more info)
        self._cache = {}
        self._hash = 0
        self._mutable = True

        # initialize and set the private metadata dictionary
        self._metadata = {}
        self.metadata = metadata

    def copy(self):
        """
        Return a deep copy of the object.
        """
        copied = deepcopy(self)
        # intersection query will return None upon deep copy
        # so let's remember to clear it from the cache
        copied._cache.pop('_iq', None)
        return copied

    def save(self, filename, fmt=None):
        """
        Write mesh to file.

        Parameters
        ----------
        filename : str
            Target filename to write array to.
        fmt : str
            Optional file format to force.
        """
        from surfa.io.mesh import save_mesh
        save_mesh(self, filename, fmt=fmt)

    @property
    def vertices(self):
        """
        Mesh 3D point positions defined by a (V, 3) array, where V corresponds to
        the number of vertices.
        """
        return self._vertices

    @vertices.setter
    def vertices(self, vertices):
        vertices = np.asanyarray(vertices, dtype=np.float64, order='C')
        check_array(vertices, ndim=2, name='vertices')
        if vertices.shape[-1] != 3:
            raise ValueError(f'expected shape (V, 3) for vertices array, but got {vertices.shape}')
        self._vertices = vertices

    @property
    def nvertices(self):
        """
        Total number of vertices in the mesh.
        """
        return len(self.vertices)

    @property
    def faces(self):
        """
        Mesh triangle faces defined by a (F, 3) array, where F corresponds to the
        total number of mesh faces.
        """
        return self._faces

    @faces.setter
    def faces(self, faces):
        faces = np.zeros((0, 3)) if faces is None else np.asanyarray(faces, dtype=np.int64, order='C')
        check_array(faces, ndim=2, name='faces')
        if faces.shape[-1] != 3:
            raise ValueError(f'expected shape (F, 3) for faces array, but got {faces.shape}')
        self._faces = faces

    @property
    def nfaces(self):
        """
        Total number of faces in the mesh.
        """
        return len(self.faces)

    @property
    def metadata(self):
        """
        Metadata dictionary.
        """
        return self._metadata

    @metadata.setter
    def metadata(self, value):
        """
        Replace the metadata dictionary. Will always make a deep copy of the new dictionary.
        """
        self._metadata = deepcopy(value) if value is not None else {}

    @property
    def space(self):
        """
        Coordinate space of the points.
        """
        return self._space

    @space.setter
    def space(self, value):
        self._space = cast_space(value, allow_none=False, copy=True)

    @property
    def geom(self):
        """
        Geometry that maps mesh coordinates to image and world spaces.
        """
        return self._geometry

    @geom.setter
    def geom(self, geometry):
        if geometry is None:
            geometry = ImageGeometry(shape=(256, 256, 256))
        else:
            geometry = cast_image_geometry(geometry, copy=True)
        setattr(self, '_geometry', geometry)

    def bbox(self):
        """
        The (min, max) coordinates defining the mesh's bounding box.
        """
        return (self.vertices.min(axis=0), self.vertices.max(axis=0))

    def convert(self, space=None, geometry=None, copy=True):
        """
        Converts mesh vertex positions into a new coordinate space, given a particular
        image geometry.

        Parameters
        ----------
        space : Space
            Target coordinate space.
        geometry : Geometry
            Target image geometry.
        copy : bool
            Return a copy of the mesh when target space and geometry conditions
            are already satisfied.s

        Returns
        -------
        Mesh
            Mesh with converted vertex positions.
        """
        space = self.space if space is None else cast_space(space)
        geometry = self.geom if geometry is None else cast_image_geometry(geometry)

        same_geom = image_geometry_equal(geometry, self.geom)
        same_space = space == self.space

        # return self if no changes are necessary
        if same_geom and same_space:
            return self.copy() if copy else self

        # make copy of the mesh
        converted = self.copy()

        if same_geom:
            # only need to transform the points once if the geometry doesn't change
            converted.vertices = self.geom.affine(self.space, space).transform(self.vertices)
            converted.space = space
        else:
            # if we're updating the geometry, we'll need to pass through world space first
            aff = geometry.affine('world', space) @ self.geom.affine(self.space, 'world')
            converted.vertices = aff(self.vertices)
            converted.space = space
            converted.geom = geometry

        return converted

    @cached_mesh_property
    def triangles(self):
        """
        Triangle coordinate arrary with shape (F, 3, 3). This parameter is
        recomputed upon retrieval if the mesh changes.
        """
        return self.vertices[self.faces]

    @cached_mesh_property
    def triangles_cross(self):
        """
        Vertex cross-product. This parameter is recomputed upon retrieval
        if the mesh changes.
        """
        vecs = np.diff(self.triangles, axis=1)
        cross = np.cross(vecs[:, 0], vecs[:, 1])
        return cross

    @cached_mesh_property
    def face_normals(self):
        """
        Face normal (unit) vectors. This parameter is recomputed upon retrieval
        if the mesh changes.
        """
        return normalize(self.triangles_cross)

    @cached_mesh_property
    def face_areas(self):
        """
        Face normal (unit) vectors. This parameter is recomputed upon retrieval
        if the mesh changes.
        """
        return np.sqrt(np.sum(self.triangles_cross ** 2, axis=-1)) / 2

    @cached_mesh_property
    def face_angles(self):
        """
        Face angles (in radians). This parameter is recomputed upon retrieval
        if the mesh changes.
        """
        triangles = self.triangles
        u = normalize(triangles[:, 1] - triangles[:, 0])
        v = normalize(triangles[:, 2] - triangles[:, 0])
        w = normalize(triangles[:, 2] - triangles[:, 1])
        angles = np.zeros((len(triangles), 3), dtype=np.float64)
        angles[:, 0] = np.arccos(np.clip(np.dot( u * v, [1.0] * 3), -1, 1))
        angles[:, 1] = np.arccos(np.clip(np.dot(-u * w, [1.0] * 3), -1, 1))
        angles[:, 2] = np.pi - angles[:, 0] - angles[:, 1]
        return angles

    @cached_mesh_property
    def vertex_normals(self):
        """
        Vertex normal (unit) vectors, with contributing face normals weighted by their
        angle. This parameter is recomputed upon retrieval if the mesh changes.
        """
        corner_angles = self.face_angles[np.repeat(np.arange(len(self.faces)), 3),
                                         np.argsort(self.faces, axis=1).ravel()]

        col = np.tile(np.arange(self.nfaces).reshape((-1, 1)), (1, 3)).reshape(-1)
        row = self.faces.reshape(-1)

        data = np.ones(len(col), dtype=bool)
        shape = (self.nvertices, self.nfaces)
        matrix = coo_matrix((data, (row, col)), shape=shape, dtype=bool).astype(np.float64)
        matrix.data = corner_angles
    
        return normalize(matrix.dot(self.face_normals))

    @cached_mesh_property
    def edges(self):
        """
        All directional edges in the mesh.
        """
        return self.faces[:, [0, 1, 1, 2, 2, 0]].reshape((-1, 2))

    @cached_mesh_property
    def edge_face(self):
        """
        Face index corresponding to each directional edge in the mesh. 
        """
        return np.tile(np.arange(self.nfaces), (3, 1)).T.reshape(-1)

    @cached_mesh_property
    def unique_face_edges(self):
        """
        Unique edge indices corresponding to the sides of each face.
        """
        return self.unique_edge_indices[1].reshape((-1, 3))

    @cached_mesh_property
    def unique_edge_indices(self):
        """
        Indices to extract all unique edges from the directional edge list.
        """
        aligned = np.sort(self.edges, axis=1)
        order = np.lexsort((aligned[:, 1], aligned[:, 0]))
        pef = aligned[order]
        shift = np.r_[True, np.any(pef[1:] != pef[:-1], axis=-1), True]
        matched = np.argwhere(shift).squeeze(-1)
        repeated = np.repeat(np.arange(len(matched) - 1), np.diff(matched))
        reverse = repeated[np.argsort(order)]
        indices = order[matched[:-1]]
        return indices, reverse

    @cached_mesh_property
    def unique_edges(self):
        """
        Unique bi-directional edges in the mesh.
        """
        return self.edges[self.unique_edge_indices[0]]

    @cached_mesh_property
    def adjacent_faces(self):
        """
        Adjacent faces that correspond to each edge in `unique_edges`.
        """
        indices = np.tile(self.unique_edge_indices[0], (1, 2))
        indices[:, 1] += 1
        return self.edge_face[indices]

    @cached_mesh_property
    def is_sphere(self):
        """
        Whether the mesh is characterized by spherical properties. The mesh must have
        a center close to zero and little variation in radii. This parameter is recomputed
        upon retrieval if the mesh changes.
        """
        return mesh_is_sphere(self)

    @cached_mesh_property
    def kdtree(self):
        """
        KD tree of the vertex structure. This parameter is recomputed upon retrieval if
        the mesh changes. The tree is represented by a `scipy.spatial.cKDTree` instance.
        """
        return cKDTree(self.vertices)

    def nearest_vertex(self, points, k=1):
        """
        Locate the nearest `k` vertices from a point or list of points.

        Parameters
        ----------
        origins : (n, 3) float
            Ray vector origin points.
        origins : (n, 3) float
            Ray vector directions (can be unnormalized).

        Returns
        -------
        faces : (n,) int
            Indices of intersected faces. Index will be -1 if intersection was not found.
        dists : (n,) float
            Distance to intersection point from ray origin.
        bary : (n, 3) float
            Barycentric weights representing the intersection point on the triangle face.
        """
        dist, nn = self.kdtree.query(points, k=k, workers=-1)
        return (nn, dist)

    @cached_mesh_property
    def _iq(self):
        """
        Cached intersection query for ray-tracing.
        """
        return RayIntersectionQuery(self)

    def ray_intersection(self, origins, dirs):
        """
        Compute intersections between rays and mesh triangles.

        Parameters
        ----------
        origins : (n, 3) float
            Ray vector origin points.
        origins : (n, 3) float
            Ray vector directions (can be unnormalized).

        Returns
        -------
        faces : (n,) int
            Indices of intersected faces. Index will be -1 if intersection was not found.
        dists : (n,) float
            Distance to intersection point from ray origin.
        bary : (n, 3) float
            Barycentric weights representing the intersection point on the triangle face.
        """
        return self._iq.ray_intersection(origins, dirs)

    def smooth_overlay(self, overlay, iters=10, step=0.5, weighted=True, pinned=None):
        """
        Smooth the scalar values of an overlay along the mesh.

        Parameters
        ----------
        overlay : Overlay
            Overlay to smooth.
        iters : int
            Number of smoothing iterations.
        step : float
            The step rate of the smoothing. This controls how much to weight the
            contribution of the neighboring vertices at each smoothing iteration.
        weighted : bool
            Whether the contribution of each vertex neighbor is weighted
            by its inverse distance from the target vertex. Otherwise, all
            neighbors are weighted equally.
        pinned : ndarray or Overlay
            Mask of pinned (unchanging) values in the mesh.

        Returns
        -------
        overlay : Overlay
            Smoothed mesh overlay.
        """
        neighborhood = self.sparse_neighborhood(weighted)

        overlay = cast_overlay(overlay)
        smoothed = overlay.data.copy()

        if pinned is not None:
            moving = pinned == 0

        for _ in range(iters):
            dot = neighborhood.dot(smoothed) - smoothed
            if pinned is not None:
                smoothed[moving] += step * dot[moving]
            else:
                smoothed += step * dot

        return overlay.new(smoothed)

    def sparse_neighborhood(self, weighted=True):
        """
        Compute a COO sparse matrix representing the immediate neighborhood
        of vertices around each vertex. Matrix values indicate the weight of
        each neighbor contribution to the target vertex.

        Parameters
        ----------
        weighted : bool
            Whether the contribution of each vertex neighbor is weighted
            by its inverse distance from the target vertex. Otherwise, all
            neighbors are weighted equally.

        Returns
        -------
        sparse : scipy.sparse.coo_matrix
        """
        row = self.edges[:, 0]
        col = self.edges[:, 1]

        # determine how to weight each neighbor
        if weighted:
            diff = self.vertices[row] - self.vertices[col]
            data = 1 / np.sqrt((diff ** 2).sum(-1))
        else:
            data = np.ones(len(row))

        # build the matrix
        sparse = coo_matrix((data, (row, col)), shape=[self.nvertices] * 2)

        # we'll want to normalize each row
        sparse.data /= sparse.sum(-1).A1[sparse.row]

        return sparse

    def face_to_vertex_overlay(self, overlay, method='mean'):
        """
        Convert a face-specific overlay to a vertex-specific overlay.

        Parameters
        ----------
        overlay : Overlay
            Face overlay to convert, must have points equal to the number
            of mesh faces.
        method: {'mean', 'min', 'max'}
            Reduction method to gather face scalars over vertices.

        Returns
        -------
        scalars : Overlay
        """
        if overlay.shape[0] != self.nfaces:
            raise ValueError(f'expected overlay to have {self.nfaces} points to match '
                             f'the number of mesh faces, but instead got {overlay.shape[0]}')
        overlay = cast_overlay(overlay)
  
        # 
        method = str(method).lower()
        if method == 'mean':
            buffer = np.zeros(self.nvertices)
            np.add.at(buffer, self.faces[:, 0], overlay)
            np.add.at(buffer, self.faces[:, 1], overlay)
            np.add.at(buffer, self.faces[:, 2], overlay)
            buffer /= np.bincount(self.faces.flat)
        elif method == 'max':
            buffer = np.full(self.nvertices, overlay.min(), dtype=overlay.dtype)
            np.maximum.at(buffer, self.faces[:, 0], overlay)
            np.maximum.at(buffer, self.faces[:, 1], overlay)
            np.maximum.at(buffer, self.faces[:, 2], overlay)
        elif method == 'min':
            buffer = np.full(self.nvertices, overlay.max(), dtype=overlay.dtype)
            np.minimum.at(buffer, self.faces[:, 0], overlay)
            np.minimum.at(buffer, self.faces[:, 1], overlay)
            np.minimum.at(buffer, self.faces[:, 2], overlay)
        else:
            raise ValueError(f'unknown method `{method}`, expected one of: mean/min/max')

        return overlay.new(buffer)

    def vertex_to_face_overlay(self, overlay, method='mean'):
        """
        Convert a vertex-specific overlay to a face-specific overlay.

        Parameters
        ----------
        overlay : Overlay
            Vertex overlay to convert, must have points equal to the number
            of mesh vertices.
        method: {'mean', 'min', 'max'}
            Reduction method to gather vertex scalars over faces.

        Returns
        -------
        scalars : Overlay
        """
        if overlay.shape[0] != self.nvertices:
            raise ValueError(f'expected overlay to have {self.nvertices} points to match '
                             f'the number of mesh vertices, but instead got {overlay.shape[0]}')
        overlay = cast_overlay(overlay)

        # 
        gathered = overlay[self.faces]

        # 
        method = str(method).lower()
        if method == 'mean':
            reduced = gathered.mean(1)
        elif method == 'max':
            reduced = gathered.max(1)
        elif method == 'min':
            reduced = gathered.min(1)
        else:
            raise ValueError(f'unknown method `{method}`, expected one of: mean/min/max')

        return overlay.new(reduced)

    def find_self_intersecting_faces(self, knn=50, mask=False):
        """
        Locate any faces in the mesh topology that are self-intersecting with each other.
        To fix these intersections, see `mesh.remove_self_intersections`.

        Parameters
        ----------
        knn : input
            Number of nearest face neighbors to compute intersections with. The default value
            should be sufficient for most meshes.
        mask : bool
            If this is enabled, the return value will be a face overlay marking intersecting faces.

        Returns
        -------
        intersecting : int array or Overlay
            Returns an integer array listing the indices of intersecting faces, unless `mask` is enabled,
            in which case the function returns a boolean overlay marking the intersecting faces.
        """

        # we want to compute an triangle intersection test between nearby faces, so
        # build a kd tree for the centers of each triangle and lookup closest pairs.
        # the intesection code will be smart enough to ignore self-referencing hits
        # as well as immediate neighboring faces
        centers = self.triangles.mean(1)
        _, neighbors = cKDTree(centers).query(centers, k=knn, workers=-1)

        # given this list of neighboring faces, compute triangle-triangle intersections
        neighbors = np.ascontiguousarray(neighbors.astype(np.int32))
        selected = np.arange(self.nfaces, dtype=np.int32)
        intersecting = triangle_intersections(self.vertices, self.faces.astype(np.int32), selected, neighbors)

        # option to return as a face overlay or just lists of face indices
        if mask:
            return intersecting
        return selected[intersecting]

    def remove_self_intersections(self, smoothing_iters=2, global_iters=50, knn=50):
        """
        Remove self-intersecting faces in the mesh by smoothing the vertex positions
        of offending triangles.

        Parameters
        ----------
        knn : input
            Number of nearest face neighbors to compute intersections with. The default value
            should be sufficient for most meshes.
        mask : bool
            If this is enabled, the return value will be a face overlay marking intersecting faces.

        Returns
        -------
        intersecting : int array or Overlay
            Returns an integer array listing the indices of intersecting faces, unless `mask` is enabled,
            in which case the function returns a boolean overlay marking the intersecting faces.
        """
        vertices = self.vertices.astype(np.float64, copy=False)
        faces = self.faces.astype(np.int32, copy=False)

        # we loop over a set of global iterations which begin by checking for intersections
        # within the entire mesh
        for iteration in range(global_iters):
            # we want to compute an triangle intersection test between nearby faces, so
            # build a kd tree for the centers of each triangle and lookup closest pairs.
            # the intesection code will be smart enough to ignore self-referencing hits
            # as well as immediate neighboring faces
            centers = vertices[faces].mean(1)
            _, neighbors = cKDTree(centers).query(centers, k=knn, workers=-1)
            neighbors = np.ascontiguousarray(neighbors).astype(np.int32)
            selected = np.arange(self.nfaces).astype(np.int32)

            # within each global loop is a set of local iterations, which operate only on a narrowed
            # down a set of intersecting faces
            for step in range(10):
                # compute triangle-triangle intersections only on the selected faces
                intersecting = triangle_intersections(vertices, faces, selected, neighbors[selected])

                # break if no intersections and if this is the first global
                # iteration, then we're good to go
                nintersections = np.count_nonzero(intersecting)
                if nintersections == 0:
                    if step == 0:
                        fixed = self.copy()
                        fixed.vertices = vertices
                        return fixed
                    break

                # attempt to remove the intersections by the smoothing the vertices associates
                # with the troublesome faces
                # TODO: this smoothing operates over the entire mesh and could be sped up a bunch
                selected = selected[intersecting]
                pinned = np.ones(self.nfaces)
                pinned[selected] = 0
                pinned = self.face_to_vertex_overlay(pinned, method='min')
                vertices = self.smooth_overlay(vertices, iters=smoothing_iters, pinned=pinned).data

        # bad news if we got this far
        print(f'Warning: Could not completely fix face intersections within {max_iterations} iterations. '
              f'Resulting mesh still contains {nintersections} intersections.')
        unfixed = self.copy()
        unfixed.vertices = vertices
        return unfixed
