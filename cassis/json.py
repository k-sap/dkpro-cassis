import base64
import json
import warnings
from collections import OrderedDict, defaultdict
from io import BytesIO, TextIOWrapper
from typing import IO, Dict, Iterable, List, Optional, Set, Union

import attr
from lxml import etree

from cassis.cas import Cas, IdGenerator, Sofa, View
from cassis.typesystem import FeatureStructure, TypeNotFoundError, TypeSystem

RESERVED_FIELD_PREFIX = "%"
TYPE_FIELD = RESERVED_FIELD_PREFIX + "TYPE"
RANGE_FIELD = RESERVED_FIELD_PREFIX + "RANGE"
TYPES_FIELD = RESERVED_FIELD_PREFIX + "TYPES"
FEATURES_FIELD = RESERVED_FIELD_PREFIX + "FEATURES"
VIEWS_FIELD = RESERVED_FIELD_PREFIX + "VIEWS"
VIEW_SOFA_FIELD = RESERVED_FIELD_PREFIX + "SOFA"
VIEW_INDEX_FIELD = RESERVED_FIELD_PREFIX + "INDEX"
FEATURE_STRUCTURES_FIELD = RESERVED_FIELD_PREFIX + "FEATURE_STRUCTURES"
REF_FEATURE_PREFIX = "@"
NAME_FIELD = RESERVED_FIELD_PREFIX + "NAME"
SUPER_TYPE_FIELD = RESERVED_FIELD_PREFIX + "SUPER_TYPE"
ELEMENT_TYPE_FIELD = RESERVED_FIELD_PREFIX + "ELEMENT_TYPE"
ID_FIELD = RESERVED_FIELD_PREFIX + "ID"
FLAGS_FIELD = RESERVED_FIELD_PREFIX + "FLAGS"
FLAG_DOCUMENT_ANNOTATION = "DocumentAnnotation"
ARRAY_SUFFIX = "[]"
ELEMENTS_FIELD = RESERVED_FIELD_PREFIX + "ELEMENTS"


def load_cas_from_json(source: Union[IO, str], typesystem: TypeSystem = None) -> Cas:
    """Loads a CAS from a JSON source.

    Args:
        source: The JSON source. If `source` is a string, then it is assumed to be an JSON string.
            If `source` is a file-like object, then the data is read from it.
        typesystem: The type system that belongs to this CAS. If `None`, an empty type system is provided.
        lenient: If `True`, unknown Types will be ignored. If `False`, unknown Types will cause an exception.
            The default is `False`.

    Returns:
        The deserialized CAS

    """
    if typesystem is None:
        typesystem = TypeSystem()

    deserializer = CasJsonDeserializer()
    return deserializer.deserialize(source, typesystem=typesystem)


class CasJsonDeserializer:
    def __init__(self):
        self._max_xmi_id = 0
        self._max_sofa_num = 0
        self._post_processors = []

    def deserialize(self, source: Union[IO, str], typesystem: TypeSystem) -> Cas:
        if isinstance(source, str):
            data = json.loads(source)
        else:
            data = json.load(source)

        feature_structures = {}

        self._max_xmi_id = 0
        self._max_sofa_num = 0
        self._post_processors = []

        data.get(TYPES_FIELD)  # FIXME

        cas = Cas(typesystem=typesystem)

        json_feature_structures = data.get(FEATURE_STRUCTURES_FIELD)
        if isinstance(json_feature_structures, list):
            for json_fs in json_feature_structures:
                if json_fs.get(TYPE_FIELD) == Cas.TYPE_NAME_SOFA:
                    fs_id = json_fs.get(ID_FIELD)
                    fs = self._parse_sofa(cas, fs_id, json_fs, feature_structures)
                else:
                    fs_id = json_fs.get(ID_FIELD)
                    fs = self._parse_feature_structure(typesystem, fs_id, json_fs, feature_structures)
                feature_structures[fs.xmiID] = fs

        if isinstance(json_feature_structures, dict):
            for fs_id, json_fs in json_feature_structures.items():
                if json_fs.get(TYPE_FIELD) == Cas.TYPE_NAME_SOFA:
                    fs_id = int(fs_id)
                    fs = self._parse_sofa(cas, fs_id, json_fs, feature_structures)
                else:
                    fs_id = int(fs_id)
                    fs = self._parse_feature_structure(typesystem, fs_id, json_fs, feature_structures)
                feature_structures[fs.xmiID] = fs

        for post_processor in self._post_processors:
            post_processor()

        cas._xmi_id_generator = IdGenerator(self._max_xmi_id + 1)
        cas._sofa_num_generator = IdGenerator(self._max_sofa_num + 1)

        # At this point all views for which we have a sofa with a known ID and sofaNum have already been created
        # as part of parsing the feature structures. Thus, if there are any views remaining that are only declared
        # in the views section, we just create them with auto-assigned IDs
        json_views = data.get(VIEWS_FIELD)
        for view_name, json_view in json_views.items():
            self._parse_view(cas, view_name, json_view, feature_structures)

        return cas

    def _get_or_create_view(
        self, cas: Cas, view_name: str, fs_id: Optional[int] = None, sofa_num: Optional[int] = None
    ) -> Cas:
        if view_name == Cas.NAME_DEFAULT_SOFA:
            view = cas.get_view(Cas.NAME_DEFAULT_SOFA)

            # We need to make sure that the sofa gets the real xmi, see #155
            if fs_id is not None:
                view.get_sofa().xmiID = fs_id

            return view
        else:
            return cas.create_view(view_name, xmiID=fs_id, sofaNum=sofa_num)

    def _parse_view(self, cas: Cas, view_name: str, json_view: Dict[str, any], feature_structures: List):
        view = self._get_or_create_view(cas, view_name)
        for member_id in json_view[VIEW_INDEX_FIELD]:
            fs = feature_structures[member_id]
            view.add_annotation(fs, keep_id=True)

    def _parse_sofa(self, cas: Cas, fs_id: int, json_fs: Dict[str, any], feature_structures: Dict[int, any]) -> Sofa:
        view = self._get_or_create_view(
            cas, json_fs.get(Cas.FEATURE_BASE_NAME_SOFAID), fs_id, json_fs.get(Cas.FEATURE_BASE_NAME_SOFANUM)
        )

        view.sofa_string = json_fs.get(Cas.FEATURE_BASE_NAME_SOFASTRING)
        view.sofa_mime = json_fs.get(Cas.FEATURE_BASE_NAME_SOFAMIME)
        view.sofa_uri = json_fs.get(Cas.FEATURE_BASE_NAME_SOFAURI)
        view.sofa_array = feature_structures.get(json_fs.get(REF_FEATURE_PREFIX + Cas.FEATURE_BASE_NAME_SOFAARRAY))

        return view.get_sofa()

    def _parse_feature_structure(
        self, typesystem: TypeSystem, fs_id: int, json_fs: Dict[str, any], feature_structures: Dict[int, any]
    ):
        AnnotationType = typesystem.get_type(json_fs.get(TYPE_FIELD))

        attributes = dict(json_fs)

        # Map the JSON FS ID to xmiID
        attributes["xmiID"] = fs_id

        # Remap features that use a reserved Python name
        if "self" in attributes:
            attributes["self_"] = attributes.pop("self")

        if "type" in attributes:
            attributes["type_"] = attributes.pop("type")

        if AnnotationType.name == Cas.TYPE_NAME_BYTE_ARRAY:
            attributes["elements"] = base64.b64decode(attributes.get(ELEMENTS_FIELD))

        self._resolve_references(attributes, feature_structures)
        self._strip_reserved_json_keys(attributes)

        self._max_xmi_id = max(attributes["xmiID"], self._max_xmi_id)
        return AnnotationType(**attributes)

    def _resolve_references(self, attributes: Dict[str, any], feature_structures: Dict[int, any]):
        for key, value in list(attributes.items()):
            if key.startswith(REF_FEATURE_PREFIX):
                attributes.pop(key)
                feature_name = key[1:]
                target_fs = feature_structures.get(value)
                if target_fs:
                    # Resolve id-ref now
                    attributes[feature_name] = target_fs
                else:
                    # Resolve id-ref at the end of processing
                    def fix_up():
                        attributes[feature_name] = feature_structures.get(value)

                    self._post_processors.append(fix_up)

    def _strip_reserved_json_keys(
        self,
        attributes: Dict[str, any],
    ):
        for key in list(attributes):
            if key.startswith(RESERVED_FIELD_PREFIX):
                attributes.pop(key)


class CasJsonSerializer:
    _COMMON_FIELD_NAMES = {"xmiID", "type"}

    def __init__(self):
        pass

    def serialize(self, sink: Union[IO, str], cas: Cas, pretty_print=True):
        data = {}
        types = data[TYPES_FIELD] = {}
        views = data[VIEWS_FIELD] = {}
        feature_structures = data[FEATURE_STRUCTURES_FIELD] = []

        for view in cas.views:
            views[view.sofa.sofaID] = self._serialize_view(view)
            if view.sofa.sofaArray:
                json_sofa_array_fs = self._serialize_feature_structure(cas, view.sofa.sofaArray)
                feature_structures.append(json_sofa_array_fs)
            json_sofa_fs = self._serialize_feature_structure(cas, view.sofa)
            feature_structures.append(json_sofa_fs)

        # Find all fs, even the ones that are not directly added to a sofa
        for fs in sorted(cas._find_all_fs(), key=lambda a: a.xmiID):
            json_fs = self._serialize_feature_structure(cas, fs)
            feature_structures.append(json_fs)

        if isinstance(sink, BytesIO):
            sink = TextIOWrapper(sink, encoding="utf-8", write_through=True)

        if sink:
            json.dump(data, sink, sort_keys=False)
        else:
            json.dumps(data, sort_keys=False)

        if isinstance(sink, TextIOWrapper):
            sink.detach()  # Prevent TextIOWrapper from closing the BytesIO

    def _serialize_feature_structure(self, cas, fs) -> dict:
        json_fs = OrderedDict()
        json_fs[ID_FIELD] = fs.xmiID
        json_fs[TYPE_FIELD] = fs.type

        ts = cas.typesystem
        t = ts.get_type(fs.type)
        for feature in t.all_features:
            if feature.name in CasJsonSerializer._COMMON_FIELD_NAMES:
                continue

            feature_name = feature.name

            # Strip the underscore we added for reserved names
            if feature._has_reserved_name:
                feature_name = feature.name[:-1]

            # Skip over 'None' features
            value = getattr(fs, feature.name)
            if value is None:
                continue

            # Map back from offsets in Unicode codepoints to UIMA UTF-16 based offsets
            # if ts.is_instance_of(fs.type, "uima.tcas.Annotation") and feature_name == "begin" or feature_name == "end":
            #    sofa: Sofa = getattr(fs, "sofa")
            #    value = sofa._offset_converter.cassis_to_uima(value)

            if t.name == Cas.TYPE_NAME_BYTE_ARRAY and feature_name == "elements":
                json_fs[ELEMENTS_FIELD] = base64.b64encode(value).decode("ascii")
            elif t.supertypeName == Cas.TYPE_NAME_ARRAY_BASE and feature_name == "elements":
                json_fs[ELEMENTS_FIELD] = value
            elif ts.is_primitive(feature.rangeTypeName):
                json_fs[feature_name] = value
            elif ts.is_collection(fs.type, feature):
                json_fs[REF_FEATURE_PREFIX + feature_name] = value.xmiID
            else:
                # We need to encode non-primitive features as a reference
                json_fs[REF_FEATURE_PREFIX + feature_name] = value.xmiID
        return json_fs

    def _serialize_view(self, view: View):
        return {VIEW_SOFA_FIELD: view.sofa.xmiID, VIEW_INDEX_FIELD: sorted(x.xmiID for x in view.get_all_annotations())}
