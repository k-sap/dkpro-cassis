from collections import defaultdict
from io import BytesIO
import itertools
from pathlib import Path
import sys
from typing import Dict, Iterable, Iterator, List, Union, Tuple, Optional

import attr
from attr import validators

from sortedcontainers import SortedList, SortedKeyList

from cassis.typesystem import FeatureStructure, TypeSystem

import deprecation

_validator_optional_string = validators.optional(validators.instance_of(str))


class IdGenerator:
    def __init__(self, initial_id: int = 1):
        self._next_id = initial_id

    def generate_id(self) -> int:
        result = self._next_id
        self._next_id += 1
        return result


@attr.s(slots=True)
class Sofa:
    """Each CAS has one or more Subject of Analysis (SofA)"""

    #: int: The sofaNum
    sofaNum = attr.ib(validator=validators.instance_of(int))

    #: int: The XMI id
    xmiID = attr.ib(validator=validators.instance_of(int))

    #: str: The name of the sofa, i.e. the sofa ID
    sofaID = attr.ib(validator=validators.instance_of(str))

    #: str: The text corresponding to this sofa
    sofaString = attr.ib(default=None, validator=_validator_optional_string)

    #: str: The mime type of `sofaString`
    mimeType = attr.ib(default=None, validator=_validator_optional_string)

    #: str: The sofa URI, it references remote sofa data
    sofaURI = attr.ib(default=None, validator=_validator_optional_string)


class View:
    """A view into a CAS contains a subset of feature structures and annotations."""

    def __init__(self, sofa: Sofa):
        """ Creates a new view for the given sofa.

        Args:
            sofa: The sofa associated with this view.
        """
        self.sofa = sofa

        # Annotations are sorted by begin index first (smaller first). If begin
        # is equal, sort by end index, smaller first. This is the same as
        # comparing a Python tuple of (begin, end)
        self._indices = defaultdict(lambda: SortedKeyList(key=_sort_func))

    @property
    def type_index(self) -> Dict[str, SortedKeyList]:
        """ Returns an index mapping type names to annotations of this type.

        Returns:
            A dictionary mapping type names to annotations of this type.
        """
        return self._indices

    def add_annotation_to_index(self, annotation):
        self._indices[annotation.type].add(annotation)

    def get_all_annotations(self) -> Iterator[FeatureStructure]:
        """ Gets all the annotations in this view.

        Returns:
            An iterator over all annotations in this view.

        """
        for annotations_by_type in self._indices.values():
            yield from annotations_by_type


class Index:
    def __init__(self, typesystem: TypeSystem):
        self._data = SortedKeyList(key=_sort_func)
        self._typesystem = typesystem


class Cas:
    """A CAS object is a container for text (sofa) and annotations"""

    def __init__(self, typesystem: TypeSystem = None):
        """ Creates a CAS with the specified typesystem. If no typesystem is given, then the default one
        is used which only contains UIMA-predefined types.

        Args:
            typesystem: The types system to use.
        """
        self._typesystem = typesystem if typesystem else TypeSystem()

        # When new attributes are added, they also need to be added in Cas::_copy. The copying
        # relies on the fact that all the members of the Cas are mutable references. It is not
        # possible right now to add not-mutable references because the view functionality heavily
        # relies on this functionality.
        self._sofas = {}
        self._views = {}

        self._xmi_id_generator = IdGenerator()
        self._sofa_num_generator = IdGenerator()

        # Every CAS comes with a an initial view called `_InitialView`
        self._add_view("_InitialView")
        self._current_view = self._views["_InitialView"]  # type: View

    @property
    def typesystem(self) -> TypeSystem:
        return self._typesystem

    def create_view(self, name: str) -> "Cas":
        """ Create a view and its underlying Sofa (subject of analysis).

        Args:
            name: The name of the view. This is the same as the associated Sofa name.

        Returns:
            The newly created view.

        Raises:
            ValueError: If a view with `name` already exists.
        """
        if name in self._views:
            raise ValueError("A view with name [{name}] already exists!".format(name=name))

        self._add_view(name)
        return self.get_view(name)

    def _add_view(self, name: str):
        # Create sofa
        sofa = Sofa(xmiID=self._get_next_xmi_id(), sofaNum=self._get_next_sofa_num(), sofaID=name)

        # Create view
        view = View(sofa=sofa)

        self._views[name] = view
        self._sofas[name] = sofa

    def get_view(self, name: str) -> "Cas":
        """ Gets an existing view.

        Args:
            name: The name of the view. This is the same as the associated Sofa name.

        Returns:
            The view corresponding to `name`
        """
        if name in self._views:
            # Make a shallow copy of this CAS and just change the current view.
            result = self._copy()
            result._current_view = self._views[name]
            return result
        else:
            raise KeyError("There is no view with name [{view}] in this CAS!".format(view=name))

    @property
    def views(self) -> List[View]:
        """Finds all views that this CAS manages.

        Returns:
            The list of all views belonging to this CAS.

        """
        return list(self._views.values())

    def add_annotation(self, annotation: FeatureStructure):
        """Adds an annotation to this Cas.

        Args:
            annotation: The annotation to add.

        """
        next_id = self._get_next_xmi_id()
        annotation.xmiID = next_id
        if hasattr(annotation, "sofa"):
            annotation.sofa = self.get_sofa()

        self._current_view.add_annotation_to_index(annotation)

    def add_annotations(self, annotations: Iterable[FeatureStructure]):
        """ Adds several annotations at once to this CAS.

        Args:
            annotations: An iterable of annotations to add.

        """
        for annotation in annotations:
            self.add_annotation(annotation)

    @deprecation.deprecated(details="Use annotation.get_covered_text()")
    def get_covered_text(self, annotation: FeatureStructure) -> str:
        """ Gets the text that is covered by `annotation`.

        Args:
            annotation: The annotation whose covered text is to be retrieved.

        Returns:
            The text covered by `annotation`

        """
        sofa = self.get_sofa()
        return sofa.sofaString[annotation.begin : annotation.end]

    def select(self, type_name: str) -> Iterator[FeatureStructure]:
        """ Finds all annotations of type `type_name`.

        Args:
            type_name: The name of the type whose annotation instances are to be found

        Returns:
            An iterator over all feature structures of type `type_name`

        """
        for annotation in self._get_feature_structures(type_name):
            yield annotation

    def select_covered(self, type_name: str, covering_annotation: FeatureStructure) -> Iterator[FeatureStructure]:
        """Returns an iterator over covered annotations.

        Return all annotations that are covered

        Only returns annotations that are fully covered, overlapping annotations
        are ignored.

        Args:
            type_name: The type name of the annotations to be returned
            covering_annotation: The name of the annotation which covers

        Returns:
            an iterator over covered annotations

        """
        c_begin = covering_annotation.begin
        c_end = covering_annotation.end

        for annotation in self._get_feature_structures_in_range(type_name, c_begin, c_end):
            if annotation.begin >= c_begin and annotation.end <= c_end:
                yield annotation

    def select_covering(self, type_name: str, covered_annotation: FeatureStructure) -> Iterator[FeatureStructure]:
        """Returns an iterator over annotations that cover the given annotation.

        Return all annotations that are covering. This can be potentially be slow.

        Only returns annotations that are fully covering, overlapping annotations
        are ignored.

        Args:
            type_name: The type name of the annotations to be returned
            covered_annotation: The name of the annotation which is covered

        Returns:
            an iterator over covering annotations

        """
        c_begin = covered_annotation.begin
        c_end = covered_annotation.end

        # We iterate over all annotations and check whether the provided annotation
        # is covered in the current annotation
        for annotation in self._get_feature_structures(type_name):
            if c_begin >= annotation.begin and c_end <= annotation.end:
                yield annotation

    def select_all(self) -> Iterator[FeatureStructure]:
        """Finds all feature structures in this Cas

        Returns:
            An iterator over all annotations in this Cas

        """
        return self._current_view.get_all_annotations()

    # FS handling

    def _get_feature_structures(self, type_name) -> Iterator[FeatureStructure]:
        """ Returns an iterator over all feature structures of type `type_name` and child types. """
        t = self._typesystem.get_type(type_name)
        types = {c.name for c in t.children}
        types.add(type_name)

        for name in types:
            yield from self._current_view.type_index[name]

    def _get_feature_structures_in_range(self, type_name: str, begin: int, end: int) -> Iterator[FeatureStructure]:
        """ Returns an iterator over all feature structures of type `type_name` and child types.
         Only features are returned that are in [begin, end] or close to it. If you use this function,
         you should always check bound in the calling method.
         """
        t = self._typesystem.get_type(type_name)
        types = {c.name for c in t.children}
        types.add(type_name)

        for name in types:
            annotations = self._current_view.type_index[name]

            # We use binary search to find indices for the first and last annotations that are inside
            # the window of [begin, end].
            idx_begin = max(annotations.bisect_key_left((begin, end)) - 1, 0)
            idx_end = min(annotations.bisect_key_right((end, begin)), len(annotations))

            yield from annotations[idx_begin:idx_end]

    # Sofa

    def get_sofa(self) -> Sofa:
        """ Get the Sofa feature structure associated with this CAS view.

        Returns:
            The sofa associated with this CAS view.
        """
        return self._current_view.sofa

    @property
    def sofas(self) -> List[Sofa]:
        """Finds all sofas that this CAS manages

        Returns:
            The list of all sofas belonging to this CAS

        """
        return list(self._sofas.values())

    @property
    def sofa_string(self) -> str:
        """ The sofa string contains the document text.

        Returns: The sofa string.

        """
        return self.get_sofa().sofaString

    @sofa_string.setter
    def sofa_string(self, value: str):
        """ Sets the sofa string to `value`.

        Args:
            value: The new sofa string.

        """
        self.get_sofa().sofaString = value

    @property
    def sofa_mime(self) -> str:
        """ The sofa mime contains the MIME type of the document text.

        Returns: The sofa MIME type.

        """
        return self.get_sofa().mimeType

    @sofa_mime.setter
    def sofa_mime(self, value: str):
        """ Sets the sofa MIME type to `value`.

        Args:
            value: The new sofa MIME type.

        """
        self.get_sofa().mimeType = value

    @property
    def sofa_uri(self) -> str:
        """ The sofa URI references external sofa data.

        Returns: The sofa URI.

        """
        return self.get_sofa().sofaURI

    @sofa_uri.setter
    def sofa_uri(self, value: str):
        """ Sets the sofa URI to `value`.

        Args:
            value: The new sofa MIME type.

        """
        self.get_sofa().sofaURI = value

    def to_xmi(self, path: Union[str, Path, None] = None, pretty_print: bool = False) -> Optional[str]:
        """Creates a XMI representation of this CAS.

        Args:
            path: File path, if `None` is provided the result is returned as a string
            pretty_print: `True` if the resulting XML should be pretty-printed, else `False`


        Returns:
            If `path` is None, then the XMI representation of this CAS is returned as a string

        """
        from cassis.xmi import CasXmiSerializer

        serializer = CasXmiSerializer()

        # If `path` is None, then serialize to a string and return it
        if path is None:
            sink = BytesIO()
            serializer.serialize(sink, self, pretty_print=pretty_print)
            return sink.getvalue().decode("utf-8")
        elif isinstance(path, str):
            with open(path, "wb") as f:
                serializer.serialize(f, self, pretty_print=pretty_print)
        elif isinstance(path, Path):
            with path.open("wb") as f:
                serializer.serialize(f, self, pretty_print=pretty_print)
        else:
            raise TypeError("`path` needs to be one of [str, None, Path], but was <{0}>".format(type(path)))

    def _get_next_xmi_id(self) -> int:
        return self._xmi_id_generator.generate_id()

    def _get_next_sofa_num(self) -> int:
        return self._sofa_num_generator.generate_id()

    def _copy(self) -> "Cas":
        result = Cas(self._typesystem)
        result._views = self._views
        result._sofas = self._sofas
        result._current_view = self._current_view
        result._sofa_num_generator = self._sofa_num_generator
        result._xmi_id_generator = self._xmi_id_generator
        return result


def _sort_func(a: FeatureStructure) -> Tuple[int, int]:
    d = a.__slots__
    if "begin" in d and "end" in d:
        return (a.begin, a.end)
    else:
        return (sys.maxsize, sys.maxsize)
