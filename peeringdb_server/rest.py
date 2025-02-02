import importlib

import unidecode

from rest_framework import (routers, serializers, status, viewsets)
from rest_framework.response import Response
from rest_framework.views import exception_handler
from rest_framework.exceptions import ValidationError as RestValidationError

from django.apps import apps
from django.conf import settings
from django.core.exceptions import FieldError, ValidationError, ObjectDoesNotExist
from django.db import connection
from django.utils import timezone
from django.db.models import DateTimeField
import django_namespace_perms.rest as nsp_rest

from peeringdb_server.models import (Network)
from peeringdb_server.serializers import ParentStatusException
from peeringdb_server.api_cache import CacheRedirect, APICacheLoader

import re
import reversion
import traceback
import time

import django_namespace_perms.util as nsp
from django_namespace_perms.exceptions import *

###############################################################################


class RestRouter(routers.DefaultRouter):

    schema_title = "PeeringDB API"
    schema_url = ""
    schema_renderers = None

    routes = [
        # List route.
        routers.Route(url=r'^{prefix}{trailing_slash}$', mapping={
            'get': 'list',
            'post': 'create'
        }, name='{basename}-list', initkwargs={'suffix': 'List'}),
        # Detail route.
        routers.Route(
            url=r'^{prefix}/{lookup}{trailing_slash}$', mapping={
                'get': 'retrieve',
                'put': 'update',
                'patch': 'partial_update',
                'delete': 'destroy'
            }, name='{basename}-detail', initkwargs={'suffix': 'Instance'}),
        routers.DynamicDetailRoute(
            url=r'^{prefix}/{lookup}/{methodnamehyphen}$',
            name='{basename}-{methodnamehyphen}', initkwargs={}),
        # Dynamically generated routes.
        # Generated using @action or @link decorators on methods of the
        # viewset.
        routers.Route(url=r'^{prefix}/{lookup}/{methodname}{trailing_slash}$',
                      mapping={
                          '{httpmethod}': '{methodname}',
                      }, name='{basename}-{methodnamehyphen}', initkwargs={}),
    ]

    def __init__(self, trailing_slash=False):
        self.trailing_slash = trailing_slash and '/' or ''
        super(routers.DefaultRouter, self).__init__(trailing_slash=False)


###############################################################################


def pdb_exception_handler(exc):

    print traceback.format_exc()

    return exception_handler(exc)


class client_check(object):
    """
    decorator that can be attached to rest viewset responses and will
    generate an error response if the requesting peeringdb client
    is running a client or backend version that is incompatible with
    the server

    compatibilty is controlled via facsimile during deploy and can
    be configured in env.misc.api.compat
    """

    def __init__(self):
        self.min_version = settings.CLIENT_COMPAT.get("client").get("min")
        self.max_version = settings.CLIENT_COMPAT.get("client").get("max")
        self.backends = settings.CLIENT_COMPAT.get("backends", {})

    def __call__(self, fn):
        compat_check = self.compat_check

        def wrapped(self, request, *args, **kwargs):
            try:
                compat_check(request)
            except ValueError as exc:
                return Response(status=status.HTTP_400_BAD_REQUEST,
                                data={"detail": str(exc)})

            return fn(self, request, *args, **kwargs)

        return wrapped

    def version_tuple(self, str_version):
        """ take a semantic version string and turn into a tuple """
        return tuple([int(i) for i in str_version.split(".")])

    def version_pad(self, version):
        """ take a semantic version tuple and zero pad to dev version """
        while len(version) < 4:
            version = version + (0, )
        return version

    def version_string(self, version):
        """ take a semantic version tuple and turn into a "." delimited string """
        return ".".join([str(i) for i in version])

    def backend_min_version(self, backend):
        """ return the min supported version for the specified backend """
        return self.backends.get(backend, {}).get("min")

    def backend_max_version(self, backend):
        """ return the max supported version for the specified backend """
        return self.backends.get(backend, {}).get("max")

    def client_info(self, request):
        """
        parse the useragent in the request and return client version
        info if possible.

        any connecting client that is NOT the peeringdb client will currently
        return an empty dict and not compatibility checking will be done
        """

        # if no user agent was specified in headers we bail
        try:
            agent = request.META["HTTP_USER_AGENT"]
        except KeyError:
            return {}

        # check if connecting client is peeringdb-py client and
        # if it parse
        # - the client version
        # - backend name
        # - backend version
        m = re.match("PeeringDB/([\d\.]+) (\S+)/([\d\.]+)", agent)
        if m:
            return {
                "client": self.version_tuple(m.group(1)),
                "backend": {
                    "name": m.group(2),
                    "version": self.version_tuple(m.group(3))
                }
            }
        return {}

    def compat_check(self, request):
        """
        Check if the connecting client is compatible with the api

        This is currently only sensible when the request is made through
        the official peeringdb-py client, any other client will be
        passed through without checks

        On incompatibility a ValueError is raised
        """

        info = self.client_info(request)
        compat = True
        if info:
            backend = info["backend"]["name"]

            if backend not in self.backends:
                return

            backend_min = self.backend_min_version(backend)
            backend_max = self.backend_max_version(backend)
            client_version = info.get("client")
            backend_version = info.get("backend").get("version")

            if self.version_pad(
                    self.min_version) > self.version_pad(client_version):
                # client version is too low
                compat = False
            elif self.version_pad(
                    self.max_version) < self.version_pad(client_version):
                # client version is too high
                compat = False

            if self.version_pad(backend_min) > self.version_pad(
                    backend_version):
                # client backend version is too low
                compat = False
            elif self.version_pad(backend_max) < self.version_pad(
                    backend_version):
                # client backend version is too high
                compat = False

            if not compat:
                raise ValueError(
                    "Your client version is incompatible with server version of the api, please install peeringdb>={},<={} {}>={},<={}"
                    .format(
                        self.version_string(self.min_version),
                        self.version_string(self.max_version), backend,
                        self.version_string(backend_min),
                        self.version_string(backend_max)))


###############################################################################
# VIEW SETS


class ModelViewSet(viewsets.ModelViewSet):
    """
    Generic ModelViewSet Base Class
    This should probably be moved to a common lib ?
    Ueaj
    """
    paginate_by_param = 'limit',

    # use django namespace permissions backend, this is also specified in the
    # settings but for some reason it only works when explicitly set here,
    # need to investigate
    permission_classes = (nsp_rest.BasePermission, )

    def get_queryset(self):
        """
        Prepare the queryset
        """

        qset = self.model.handleref.all()

        self.request.meta_response = {}

        if hasattr(self.serializer_class, "prepare_query"):
            try:
                qset, p_filters = self.serializer_class.prepare_query(
                    qset, **self.request.query_params)
            except ValidationError, inst:
                raise RestValidationError({"detail": str(inst[0])})
            except ValueError, inst:
                raise RestValidationError({"detail": str(inst[0])})
            except TypeError, inst:
                raise RestValidationError({"detail": str(inst[0])})
            except FieldError, inst:
                raise RestValidationError({"detail": "Invalid query"})

        else:
            p_filters = {}

        try:
            since = int(float(self.request.query_params.get('since', 0)))
        except ValueError:
            raise RestValidationError({
                "detail": "'since' needs to be a unix timestamp (epoch seconds)"
            })
        try:
            skip = int(self.request.query_params.get('skip', 0))
        except ValueError:
            raise RestValidationError({
                "detail": "'skip' needs to be a number"
            })
        try:
            limit = int(self.request.query_params.get('limit', 0))
        except ValueError:
            raise RestValidationError({
                "detail": "'limit' needs to be a number"
            })

        try:
            depth = int(self.request.query_params.get("depth", 0))
        except ValueError:
            raise RestValidationError({
                "detail": "'depth' needs to be a number"
            })

        field_names = [fld.name for fld in self.model._meta.get_fields()
                       ] + self.serializer_class.queryable_relations()

        date_fields = ["DateTimeField", "DateField"]

        # filters
        filters = {}
        for k, v in self.request.query_params.items():

            v = unidecode.unidecode(v)

            if k[-3:] == "_id" and k not in field_names:
                k = k[:-3]

            xl = self.serializer_class.queryable_field_xl

            # only apply filter if the field actually exists and uses a
            # valid suffix
            m = re.match("^(.+)__(lt|lte|gt|gte|contains|startswith|in)$", k)

            # run queryable field translation
            # on the targeted field so that the filter is actually run on
            # a field that django orm is aware of - which in most cases is
            # identical to the serializer field anyways, but in some cases it
            # may need to be substituted
            if m:
                flt = xl(m.group(1))
                k = k.replace(m.group(1), flt, 1)
                if flt[-3:] == "_id" and flt not in field_names:
                    flt = flt[:-3]
            else:
                k = xl(k)
                flt = None

            # prepare db filters
            if m and flt in field_names:
                # filter by function provided in suffix
                try:
                    intyp = self.model._meta.get_field(flt).get_internal_type()
                except:
                    intyp = "CharField"

                # for greater than date checks we want to force the time to 1
                # msecond before midnight
                if intyp in date_fields:
                    if m.group(2) in ["gt", "lte"]:
                        if len(v) == 10:
                            v = "%s 23:59:59.999" % v

                    # convert to datetime and make tz aware
                    try:
                        v = DateTimeField().to_python(v)
                    except ValidationError, inst:
                        raise RestValidationError({"detail": str(inst[0])})
                    if timezone.is_naive(v):
                        v = timezone.make_aware(v)
                    if "_ctf" in self.request.query_params:
                        self.request._ctf = {
                            "%s__%s" % (m.group(1), m.group(2)): v
                        }

                # contains should become icontains because we always
                # want it to do case-insensitive checks
                if m.group(2) == "contains":
                    filters["%s__icontains" % flt] = v
                elif m.group(2) == "startswith":
                    filters["%s__istartswith" % flt] = v
                # when the 'in' filters is found attempt to split the
                # provided search value into a list
                elif m.group(2) == "in":
                    filters[k] = v.split(",")
                else:
                    filters[k] = v
            elif k in field_names:
                # filter exact matches
                try:
                    intyp = self.model._meta.get_field(k).get_internal_type()
                except:
                    intyp = "CharField"
                if intyp == "ForeignKey":
                    filters["%s_id" % k] = v
                elif intyp == "DateTimeField" or intyp == "DateField":
                    filters["%s__startswith" % k] = v
                else:
                    filters["%s__iexact" % k] = v

        if filters:
            try:
                qset = qset.filter(**filters)
            except ValidationError, inst:
                raise RestValidationError({"detail": str(inst[0])})
            except ValueError, inst:
                raise RestValidationError({"detail": str(inst[0])})
            except TypeError, inst:
                raise RestValidationError({"detail": str(inst[0])})
            except FieldError, inst:
                raise RestValidationError({"detail": "Invalid query"})

        # check if request qualifies for a cache load
        filters.update(p_filters)
        api_cache = APICacheLoader(self, qset, filters)
        if api_cache.qualifies():
            raise CacheRedirect(api_cache)

        if not self.kwargs:
            if since > 0:
                # .filter(status__in=["ok","deleted"])
                qset = qset.since(timestamp=since,
                                  deleted=True).order_by("updated").filter(
                                      status__in=["ok", "deleted"])
            else:
                qset = qset.filter(status="ok")
        else:
            qset = qset.filter(status__in=["ok", "pending"])

        if not self.kwargs:
            if limit > 0:
                qset = qset[skip:skip + limit]
            else:
                qset = qset[skip:]

            adrl = getattr(settings, "API_DEPTH_ROW_LIMIT", 250)
            row_count = qset.count()
            if adrl and depth > 0 and row_count > adrl:
                qset = qset[:adrl]
                self.request.meta_response[
                    "truncated"] = "Your search query (with depth %d) returned more than %d rows and has been truncated. Please be more specific in your filters, use the limit and skip parameters to page through the resultset or drop the depth parameter" % (
                        depth, adrl)

        if depth > 0 or self.kwargs:
            return self.serializer_class.prefetch_related(
                qset, self.request, is_list=(len(self.kwargs) == 0))
        else:
            return qset

    @client_check()
    def list(self, request, *args, **kwargs):
        """
        ### Querying

        You may query the resultset by passing field names as url parameters

        ### Numeric Queries

        On numeric fields you can suffix the field names with the following filters:

        - \_\_lt : less-than
        - \_\_lte : less-than-equal
        - \_\_gt : greater-than
        - \_\_gte : greater-than-equal
        - \_\_in : value inside set of values (comma separated)

        **examples**

            ?<field_name>__lt=10
            ?<field_name>__in=1,10

        ### String Queries

        On string fields you can suffix the field names with the following filters:

        - \_\_contains : field value contains specified value
        - \_\_startswith : field value starts with specified value
        - \_\_in : value contained inside set of values (comma separated)

        **examples**

            ?<field_name>__contains=something
            ?<field_name>__in=this,that

        All string filtering operations are case-insensitive

        ### Since

        You can use the since argument with a unix timestamp (seconds) to retrieve all
        objects updated since then. Note that this result will contain objects that were
        deleted in that timeframe as well - you can spot them by checking for status "deleted"

        **example**

            ?since=1443414678

        ### Nested data

        Any field ending in the suffix **_set** is a list of objects in a relationship with the parent
        object, you can expand those lists with the 'depth' parameter as explained below.

        The naming schema of the field will always tell you which type of object the set is holding
        and will correspond with the object's endpoint on the API

            <object_type>_set

        So a set called 'net_set' will hold Network objects (api endpoint /net)

        ### Depth

        Nested sets will not be loaded (any field ending with the _set suffix) unless the 'depth'
        parameter is passed in the request URL.

        Depth can be one of three values:

          - 1 : expand sets into ids (slow)
          - 2 : expand sets into objects (slower)
          - 0 : dont expand sets at all (default behaviour)

        **example**

            ?depth=1

        ### Cached Responses

        Any request that does not require lookups will be served a cached result. Cache is updated approximately every 15 minutes.

        You can spot cached responses by checking for the "generated" property inside the "meta" object.

            "meta" : {
                // the cached data was last regenerated at this time (epoch)
                "generated" : 1456121358.6301942
            }

        **examples**

        will serve a cached result:

            ?depth=2

        will serve a live result:

            ?id__in=1,2

        ### Resultset limit

        Any request that does lookup queries and has it's **depth** parameter specified will have a result limit of 250 entries, any entries past this limit will be truncated, at which point you either should be more specific with your query or use the skip and limit parameters to page through the result set

        **examples**

        will serve a live result and a maximum of 250 rows at a time:

            ?updated__gt=2011-01-01&depth=1

        will serve a live result and will not be truncated:

            ?updated__gt=2011-01-01

        will serve a cached result and will not be truncated:

            ?depth=1

        ### Pagination

        Use the skip and limit parameters to page through results

            ?updated__gt=2011-01-01&depth=1&limit=250 - first page
            ?updated__gt=2011-01-01&depth=1&limit=250&skip=250 - second page
            ?updated__gt=2011-01-01&depth=1&limit=250&skip=500 - third page

        """

        t = time.time()
        try:
            r = super(ModelViewSet, self).list(request, *args, **kwargs)
        except ValueError, inst:
            return Response(status=status.HTTP_400_BAD_REQUEST,
                            data={"detail": str(inst)})
        except TypeError, inst:
            return Response(status=status.HTTP_400_BAD_REQUEST,
                            data={"detail": str(inst)})
        except CacheRedirect, inst:
            r = Response(status=200, data=inst.loader.load())
        d = time.time() - t
        print "done in %.5f seconds, %d queries" % (d, len(connection.queries))

        #FIXME: this waits for peeringdb-py fix to deal with 404 raise properly
        if not r or not len(r.data):
            if self.serializer_class.is_unique_query(request):
                return Response(
                    status=404, data={
                        "data": [],
                        "detail": "Entity not found"
                    })

        return r

    @client_check()
    def retrieve(self, request, *args, **kwargs):
        # could add fk relationships here, one at a time, but we need to define
        # them somewhere by the time we get the serializer, the data is already
        # populated

        t = time.time()
        r = super(ModelViewSet, self).retrieve(request, *args, **kwargs)
        d = time.time() - t
        print "done in %.5f seconds, %d queries" % (d, len(connection.queries))

        return r

    @client_check()
    def create(self, request, *args, **kwargs):
        """
        Create object
        """
        try:
            with reversion.create_revision():
                if request.user:
                    reversion.set_user(request.user)
                return super(ModelViewSet, self).create(
                    request, *args, **kwargs)
        except PermissionDenied, inst:
            return Response(status=status.HTTP_403_FORBIDDEN)
        except ParentStatusException, inst:
            return Response(status=status.HTTP_400_BAD_REQUEST,
                            data={"detail": str(inst)})
        finally:
            self.get_serializer().finalize_create(request)

    @client_check()
    def update(self, request, *args, **kwargs):
        """
        Update object
        """
        try:
            with reversion.create_revision():
                if request.user:
                    reversion.set_user(request.user)

                return super(ModelViewSet, self).update(
                    request, *args, **kwargs)
        except TypeError, inst:
            return Response(status=status.HTTP_400_BAD_REQUEST,
                            data={"detail": str(inst)})
        except ValueError, inst:
            return Response(status=status.HTTP_400_BAD_REQUEST,
                            data={"detail": str(inst)})
        finally:
            self.get_serializer().finalize_update(request)

    def partial_update(self, request, *args, **kwargs):
        """
        PATCH (partial update) is currently disabled
        """
        return Response(status=status.HTTP_403_FORBIDDEN)

    @client_check()
    def destroy(self, request, pk, format=None):
        """
        Delete object
        """
        try:
            try:
                obj = self.model.objects.get(pk=pk)
            except ValueError:
                return Response(status=status.HTTP_400_BAD_REQUEST,
                                data={"extra": "Invalid id"})
            except self.model.DoesNotExist:
                return Response(status=status.HTTP_204_NO_CONTENT)

            if nsp.has_perms(request.user, obj, "delete"):
                with reversion.create_revision():
                    if request.user:
                        reversion.set_user(request.user)
                    obj.delete()
                return Response(status=status.HTTP_204_NO_CONTENT)
            else:
                return Response(status=status.HTTP_403_FORBIDDEN)
        finally:
            self.get_serializer().finalize_delete(request)


pdb_serializers = importlib.import_module('peeringdb_server.serializers')
router = RestRouter(trailing_slash=False)

# router helpers


def ref_dict():
    return {tag: view.model for tag, view, na in router.registry}


def model_view_set(model):
    """
    shortcut for peeringdb models to generate viewset and register in the API urls
    """

    # lookup Serializer class
    scls = getattr(pdb_serializers, model + 'Serializer')

    model_t = apps.get_model('peeringdb_server', model)

    # setup class attributes
    clsdict = {
        'model': model_t,
        'serializer_class': scls,
        '__doc__': "Rest API endpoint for " + model,
    }

    # create the type
    viewset_t = type(model + 'ViewSet', (ModelViewSet, ), clsdict)

    # register with the rest router for incoming requests
    ref_tag = model_t.handleref.tag
    router.register(ref_tag, viewset_t, base_name=ref_tag)

    return viewset_t


FacilityViewSet = model_view_set('Facility')
InternetExchangeViewSet = model_view_set('InternetExchange')
InternetExchangeFacilityViewSet = model_view_set('InternetExchangeFacility')
IXLanViewSet = model_view_set('IXLan')
IXLanPrefixViewSet = model_view_set('IXLanPrefix')
NetworkViewSet = model_view_set('Network')
NetworkContactViewSet = model_view_set('NetworkContact')
NetworkFacilityViewSet = model_view_set('NetworkFacility')
NetworkIXLanViewSet = model_view_set('NetworkIXLan')
OrganizationViewSet = model_view_set('Organization')


class ReadOnlyMixin(object):

    def destroy(self, request, pk, format=None):
        """
        This endpoint is readonly
        """
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def create(self, request, *args, **kwargs):
        """
        This endpoint is readonly
        """
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def update(self, request, *args, **kwargs):
        """
        This endpoint is readonly
        """
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def patch(self, request, *args, **kwargs):
        """
        This endpoint is readonly
        """
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)



class ASSetViewSet(ReadOnlyMixin, viewsets.ModelViewSet):
    """
    AS-SET endpoint

    lists all as sets mapped by asn
    """

    lookup_field = "asn"
    http_method_names = ["get"]
    model = Network

    def get_queryset(self):
        return Network.objects.filter(status="ok").exclude(irr_as_set="")

    def list(self, request):
        return Response(Network.as_set_map(self.get_queryset()))

    def retrieve(self, request, asn):
        try:
            network = Network.objects.get(asn=int(asn))
        except ValueError:
            return Response(status=status.HTTP_400_BAD_REQUEST,
                            data={"detail": "Invalid ASN"})
        except ObjectDoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response({network.asn : network.irr_as_set})



router.register('as_set', ASSetViewSet, base_name='as_set')

# set here in case we want to add more urls later
urls = router.urls

REFTAG_MAP = dict([(cls.model.handleref.tag, cls) for cls in [
    OrganizationViewSet, NetworkViewSet, FacilityViewSet,
    InternetExchangeViewSet, InternetExchangeFacilityViewSet,
    NetworkFacilityViewSet, NetworkIXLanViewSet, NetworkContactViewSet,
    IXLanViewSet, IXLanPrefixViewSet
]])
