from collective.elasticsearch.indexes import EZCTextIndex
from collective.elasticsearch.indexes import getIndex
from collective.elasticsearch.interfaces import IQueryAssembler
from collective.elasticsearch.utils import getESOnlyIndexes
from zope.interface import implementer


@implementer(IQueryAssembler)
class QueryAssembler:
    def __init__(self, request, es):
        self.es = es
        self.catalog = es.catalog
        self.request = request

    def normalize(self, query):  # NOQA R0201
        sort_on = []
        sort = query.pop("sort_on", None)
        # default plone is ascending
        sort_order = query.pop("sort_order", "asc")
        if sort_order in ("descending", "reverse", "desc"):
            sort_order = "desc"
        else:
            sort_order = "asc"

        if sort:
            for sort_str in sort.split(","):
                sort_on.append({sort_str: {"order": sort_order}})
        sort_on.append("_score")
        if "b_size" in query:
            del query["b_size"]
        if "b_start" in query:
            del query["b_start"]
        if "sort_limit" in query:
            del query["sort_limit"]
        return query, sort_on

    def __call__(self, dquery):
        filters = []
        matches = []
        catalog = self.catalog._catalog
        idxs = catalog.indexes.keys()
        query_type = self.es.query_type
        query = {"match_all": {}}
        es_only_indexes = getESOnlyIndexes()
        values = False
        for key, value in dquery.items():
            if key not in idxs and key not in es_only_indexes:
                continue
            index = getIndex(catalog, key, query_type=query_type)
            if index is None and key in es_only_indexes:
                # deleted index for plone performance but still need on ES
                index = EZCTextIndex(catalog, key, query_type=query_type)
            qq = index.get_query(key, value)
            qv = None
            if type(value) == str:
                qv = value
            if type(value) == dict:
                qv = value.get('query', '')

            if key in es_only_indexes and len(qv) > 0:
                values = True
            if qq is None:
                continue
            if index is not None and index.filter_query:
                if isinstance(qq, list):
                    filters.extend(qq)
                else:
                    filters.append(qq)
            else:
                if isinstance(qq, list):
                    matches.extend(qq)
                else:
                    matches.append(qq)
        if len(filters) == 0 and len(matches) == 0:
            return query
        query = {"bool": {}}

        # To add support for has_child queries we check settings and see if
        # the query is constrained by a matching parent type.
        has_child = False
        child_filters = []
        if self.es.map_parent_child and self.es.parent_types:
            for f in filters:
                if f.get('terms') and f['terms'].get('portal_type'):
                    for x in self.es.parent_types:
                        if x in f['terms']['portal_type']:
                            has_child = True

        if len(filters) > 0:

            # Some facets should be against the child. Here we parse the
            # child_facets settings and move them accordingly. They are added
            # to the query later.
            if has_child:
                parent_filters = []
                for f in filters:
                    if f.get('terms') and [x for x in f['terms'].keys()][0] in self.es.child_facets:
                        child_filters.append(f)
                    else:
                        parent_filters.append(f)
                query["bool"]["filter"] = parent_filters
            else:
                query["bool"]["filter"] = filters

        if len(matches) > 0:
            if has_child:
                highlight_filter_field = self.es.highlight_filter_field
                highlight_filter_value = self.es.highlight_filter_value
                use_highlight_filter = bool(highlight_filter_field and highlight_filter_value)

                if use_highlight_filter:
                    # Two has_child approach: one in filter for matching
                    # (all languages), one in should for scoring and
                    # filtered highlights.

                    # 1. Matching has_child in filter — ensures parents with
                    #    any matching children appear in results.
                    matching_has_child = {
                        "has_child": {
                            "type": "child",
                            "query": {
                                "bool": {
                                    "should": matches
                                }
                            }
                        }
                    }
                    if len(child_filters) > 0:
                        matching_has_child["has_child"]["query"]["bool"]["filter"] = child_filters

                    if not isinstance(query["bool"].get("filter"), list):
                        query["bool"]["filter"] = []
                    query["bool"]["filter"].append(matching_has_child)

                    # 2. Highlight has_child in should — scores and produces
                    #    inner_hits only from the configured field/value.
                    highlight_has_child = {
                        "has_child": {
                            "type": "child",
                            "score_mode": "max",
                            "query": {
                                "bool": {
                                    "filter": [
                                        {"term": {highlight_filter_field: highlight_filter_value}}
                                    ],
                                    "should": matches,
                                    "minimum_should_match": 1
                                }
                            }
                        }
                    }

                    if self.es.highlight and values:
                        highlight_has_child["has_child"]["inner_hits"] = {
                            "highlight": {
                                "fields": {"SearchableText": {"number_of_fragments": 5}},
                                "pre_tags": self.es.highlight_pre_tags.split("\n"),
                                "post_tags": self.es.highlight_post_tags.split("\n"),
                            }
                        }

                    query["bool"]["should"] = highlight_has_child

                    # Matching is handled by the filter has_child so the
                    # should is optional (provides scoring/highlights only).
                    query["bool"]["minimum_should_match"] = 0

                else:
                    # Original single has_child approach
                    query["bool"]["should"] = {
                        "has_child": {
                            "type": "child",
                            "score_mode": "max",
                            "query": {
                                "bool": {
                                    "should": matches
                                }
                            }
                        }
                    }

                    if len(child_filters) > 0:
                        query["bool"]["should"]["has_child"]["query"]["bool"]["filter"] = child_filters

                    if self.es.highlight and values:
                        query["bool"]["should"]["has_child"]["inner_hits"] = {
                            "highlight": {
                                "fields": {"SearchableText": {"number_of_fragments": 5}},
                                "pre_tags": self.es.highlight_pre_tags.split("\n"),
                                "post_tags": self.es.highlight_post_tags.split("\n"),
                            }
                        }

                    # If there is no SearchableText query or filters then all
                    # docs should be returned (ie: a listing)
                    query["bool"]["minimum_should_match"] = 0
                    if values or len(child_filters) > 0:
                        query["bool"]["minimum_should_match"] = 1
            else:
                query["bool"]["should"] = matches

                query["bool"]["minimum_should_match"] = 0
                if values or len(child_filters) > 0:
                    query["bool"]["minimum_should_match"] = 1
        return query
