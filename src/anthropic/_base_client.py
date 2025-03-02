from __future__ import annotations

import json
import time
import uuid
import inspect
import platform
import ssl
from types import TracebackType
from random import random
from typing import (
    Any,
    Dict,
    Type,
    Union,
    Generic,
    Mapping,
    TypeVar,
    Iterable,
    Iterator,
    Optional,
    Generator,
    AsyncIterator,
    cast,
    overload,
)
from functools import lru_cache
from typing_extensions import Literal, get_origin

import anyio
import httpx
import distro
import pydantic
from httpx import URL, Limits
from pydantic import PrivateAttr

from . import _base_exceptions as exceptions
from ._qs import Querystring
from ._types import (
    NOT_GIVEN,
    Body,
    Omit,
    Query,
    ModelT,
    Headers,
    Timeout,
    NoneType,
    NotGiven,
    ResponseT,
    Transport,
    AnyMapping,
    ProxiesTypes,
    RequestFiles,
    RequestOptions,
    UnknownResponse,
    ModelBuilderProtocol,
)
from ._utils import is_dict, is_mapping
from ._compat import model_copy
from ._models import (
    BaseModel,
    GenericModel,
    FinalRequestOptions,
    validate_type,
    construct_type,
)
from ._streaming import Stream, AsyncStream
from ._base_exceptions import (
    APIStatusError,
    APITimeoutError,
    APIConnectionError,
    APIResponseValidationError,
)

# TODO: make base page type vars covariant
SyncPageT = TypeVar("SyncPageT", bound="BaseSyncPage[Any]")
AsyncPageT = TypeVar("AsyncPageT", bound="BaseAsyncPage[Any]")


_T = TypeVar("_T")
_T_co = TypeVar("_T_co", covariant=True)

_StreamT = TypeVar("_StreamT", bound=Stream[Any])
_AsyncStreamT = TypeVar("_AsyncStreamT", bound=AsyncStream[Any])


# default timeout is 10 minutes
DEFAULT_TIMEOUT = Timeout(timeout=600.0, connect=5.0)
DEFAULT_MAX_RETRIES = 2
DEFAULT_LIMITS = Limits(max_connections=100, max_keepalive_connections=20)


class MissingStreamClassError(TypeError):
    def __init__(self) -> None:
        super().__init__(
            "The `stream` argument was set to `True` but the `stream_cls` argument was not given. See `anthropic._streaming` for reference",
        )


class PageInfo:
    """Stores the necesary information to build the request to retrieve the next page.

    Either `url` or `params` must be set.
    """

    url: URL | NotGiven
    params: Query | NotGiven

    @overload
    def __init__(
        self,
        *,
        url: URL,
    ) -> None:
        ...

    @overload
    def __init__(
        self,
        *,
        params: Query,
    ) -> None:
        ...

    def __init__(
        self,
        *,
        url: URL | NotGiven = NOT_GIVEN,
        params: Query | NotGiven = NOT_GIVEN,
    ) -> None:
        self.url = url
        self.params = params


class BasePage(GenericModel, Generic[ModelT]):
    _options: FinalRequestOptions = PrivateAttr()
    _model: Type[ModelT] = PrivateAttr()

    def has_next_page(self) -> bool:
        items = self._get_page_items()
        if not items:
            return False
        return self.next_page_info() is not None

    def next_page_info(self) -> Optional[PageInfo]:
        ...

    def _get_page_items(self) -> Iterable[ModelT]:  # type: ignore[empty-body]
        ...

    def _params_from_url(self, url: URL) -> httpx.QueryParams:
        # TODO: do we have to preprocess params here?
        return httpx.QueryParams(cast(Any, self._options.params)).merge(url.params)

    def _info_to_options(self, info: PageInfo) -> FinalRequestOptions:
        options = model_copy(self._options)

        if not isinstance(info.params, NotGiven):
            options.params = {**options.params, **info.params}
            return options

        if not isinstance(info.url, NotGiven):
            params = self._params_from_url(info.url)
            url = info.url.copy_with(params=params)
            options.params = dict(url.params)
            options.url = str(url)
            return options

        raise ValueError("Unexpected PageInfo state")


class BaseSyncPage(BasePage[ModelT], Generic[ModelT]):
    _client: SyncAPIClient = pydantic.PrivateAttr()

    def _set_private_attributes(
        self,
        client: SyncAPIClient,
        model: Type[ModelT],
        options: FinalRequestOptions,
    ) -> None:
        self._model = model
        self._client = client
        self._options = options

    # Pydantic uses a custom `__iter__` method to support casting BaseModels
    # to dictionaries. e.g. dict(model).
    # As we want to support `for item in page`, this is inherently incompatible
    # with the default pydantic behaviour. It is not possible to support both
    # use cases at once. Fortunately, this is not a big deal as all other pydantic
    # methods should continue to work as expected as there is an alternative method
    # to cast a model to a dictionary, model.dict(), which is used internally
    # by pydantic.
    def __iter__(self) -> Iterator[ModelT]:  # type: ignore
        for page in self.iter_pages():
            for item in page._get_page_items():
                yield item

    def iter_pages(self: SyncPageT) -> Iterator[SyncPageT]:
        page = self
        while True:
            yield page
            if page.has_next_page():
                page = page.get_next_page()
            else:
                return

    def get_next_page(self: SyncPageT) -> SyncPageT:
        info = self.next_page_info()
        if not info:
            raise RuntimeError(
                "No next page expected; please check `.has_next_page()` before calling `.get_next_page()`."
            )

        options = self._info_to_options(info)
        return self._client._request_api_list(self._model, page=self.__class__, options=options)


class AsyncPaginator(Generic[ModelT, AsyncPageT]):
    def __init__(
        self,
        client: AsyncAPIClient,
        options: FinalRequestOptions,
        page_cls: Type[AsyncPageT],
        model: Type[ModelT],
    ) -> None:
        self._model = model
        self._client = client
        self._options = options
        self._page_cls = page_cls

    def __await__(self) -> Generator[Any, None, AsyncPageT]:
        return self._get_page().__await__()

    async def _get_page(self) -> AsyncPageT:
        page = await self._client.request(self._page_cls, self._options)
        page._set_private_attributes(  # pyright: ignore[reportPrivateUsage]
            model=self._model,
            options=self._options,
            client=self._client,
        )
        return page

    async def __aiter__(self) -> AsyncIterator[ModelT]:
        # https://github.com/microsoft/pyright/issues/3464
        page = cast(
            AsyncPageT,
            await self,  # type: ignore
        )
        async for item in page:
            yield item


class BaseAsyncPage(BasePage[ModelT], Generic[ModelT]):
    _client: AsyncAPIClient = pydantic.PrivateAttr()

    def _set_private_attributes(
        self,
        model: Type[ModelT],
        client: AsyncAPIClient,
        options: FinalRequestOptions,
    ) -> None:
        self._model = model
        self._client = client
        self._options = options

    async def __aiter__(self) -> AsyncIterator[ModelT]:
        async for page in self.iter_pages():
            for item in page._get_page_items():
                yield item

    async def iter_pages(self: AsyncPageT) -> AsyncIterator[AsyncPageT]:
        page = self
        while True:
            yield page
            if page.has_next_page():
                page = await page.get_next_page()
            else:
                return

    async def get_next_page(self: AsyncPageT) -> AsyncPageT:
        info = self.next_page_info()
        if not info:
            raise RuntimeError(
                "No next page expected; please check `.has_next_page()` before calling `.get_next_page()`."
            )

        options = self._info_to_options(info)
        return await self._client._request_api_list(self._model, page=self.__class__, options=options)


class BaseClient:
    _client: httpx.Client | httpx.AsyncClient
    _version: str
    max_retries: int
    timeout: Union[float, Timeout, None]
    _limits: httpx.Limits
    _strict_response_validation: bool
    _idempotency_header: str | None

    def __init__(
        self,
        *,
        version: str,
        _strict_response_validation: bool,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout: float | Timeout | None = DEFAULT_TIMEOUT,
        limits: httpx.Limits,
        custom_headers: Mapping[str, str] | None = None,
        custom_query: Mapping[str, object] | None = None,
    ) -> None:
        self._version = version
        self.max_retries = max_retries
        self.timeout = timeout
        self._limits = limits
        self._custom_headers = custom_headers or {}
        self._custom_query = custom_query or {}
        self._strict_response_validation = _strict_response_validation
        self._idempotency_header = None

    def _make_status_error_from_response(
        self,
        request: httpx.Request,
        response: httpx.Response,
    ) -> APIStatusError:
        err_text = response.text.strip()
        body = err_text

        try:
            body = json.loads(err_text)
            err_msg = f"Error code: {response.status_code} - {body}"
        except Exception:
            err_msg = err_text or f"Error code: {response.status_code}"

        return self._make_status_error(err_msg, body=body, request=request, response=response)

    def _make_status_error(
        self,
        err_msg: str,
        *,
        body: object,
        request: httpx.Request,
        response: httpx.Response,
    ) -> APIStatusError:
        if response.status_code == 400:
            return exceptions.BadRequestError(err_msg, request=request, response=response, body=body)
        if response.status_code == 401:
            return exceptions.AuthenticationError(err_msg, request=request, response=response, body=body)
        if response.status_code == 403:
            return exceptions.PermissionDeniedError(err_msg, request=request, response=response, body=body)
        if response.status_code == 404:
            return exceptions.NotFoundError(err_msg, request=request, response=response, body=body)
        if response.status_code == 409:
            return exceptions.ConflictError(err_msg, request=request, response=response, body=body)
        if response.status_code == 422:
            return exceptions.UnprocessableEntityError(err_msg, request=request, response=response, body=body)
        if response.status_code == 429:
            return exceptions.RateLimitError(err_msg, request=request, response=response, body=body)
        if response.status_code >= 500:
            return exceptions.InternalServerError(err_msg, request=request, response=response, body=body)
        return APIStatusError(err_msg, request=request, response=response, body=body)

    def _remaining_retries(
        self,
        remaining_retries: Optional[int],
        options: FinalRequestOptions,
    ) -> int:
        return remaining_retries if remaining_retries is not None else options.get_max_retries(self.max_retries)

    def _build_headers(self, options: FinalRequestOptions) -> httpx.Headers:
        custom_headers = options.headers or {}
        headers_dict = _merge_mappings(self.default_headers, custom_headers)
        self._validate_headers(headers_dict, custom_headers)

        headers = httpx.Headers(headers_dict)

        idempotency_header = self._idempotency_header
        if idempotency_header and options.method.lower() != "get" and idempotency_header not in headers:
            if not options.idempotency_key:
                options.idempotency_key = self._idempotency_key()

            headers[idempotency_header] = options.idempotency_key

        return headers

    def _prepare_request(self, request: httpx.Request) -> None:
        """This method is used as a callback for mutating the `Request` object
        after it has been constructed.

        This is useful for cases where you want to add certain headers based off of
        the request properties, e.g. `url`, `method` etc.
        """
        return None

    def _build_request(
        self,
        options: FinalRequestOptions,
    ) -> httpx.Request:
        headers = self._build_headers(options)

        kwargs: dict[str, Any] = {}

        json_data = options.json_data
        if options.extra_json is not None:
            if json_data is None:
                json_data = cast(Body, options.extra_json)
            elif is_mapping(json_data):
                json_data = _merge_mappings(json_data, options.extra_json)
            else:
                raise RuntimeError(f"Unexpected JSON data type, {type(json_data)}, cannot merge with `extra_body`")

        params = _merge_mappings(self._custom_query, options.params)

        # If the given Content-Type header is multipart/form-data then it
        # has to be removed so that httpx can generate the header with
        # additional information for us as it has to be in this form
        # for the server to be able to correctly parse the request:
        # multipart/form-data; boundary=---abc--
        if headers.get("Content-Type") == "multipart/form-data":
            headers.pop("Content-Type")

            # As we are now sending multipart/form-data instead of application/json
            # we need to tell httpx to use it, https://www.python-httpx.org/advanced/#multipart-file-encoding
            if json_data:
                if not is_dict(json_data):
                    raise TypeError(
                        f"Expected query input to be a dictionary for multipart requests but got {type(json_data)} instead."
                    )
                kwargs["data"] = self._serialize_multipartform(json_data)

        # TODO: report this error to httpx
        request = self._client.build_request(  # pyright: ignore[reportUnknownMemberType]
            headers=headers,
            timeout=self.timeout if isinstance(options.timeout, NotGiven) else options.timeout,
            method=options.method,
            url=options.url,
            # the `Query` type that we use is incompatible with qs'
            # `Params` type as it needs to be typed as `Mapping[str, object]`
            # so that passing a `TypedDict` doesn't cause an error.
            # https://github.com/microsoft/pyright/issues/3526#event-6715453066
            params=self.qs.stringify(cast(Mapping[str, Any], params)) if params else None,
            json=json_data,
            files=options.files,
            **kwargs,
        )
        self._prepare_request(request)
        return request

    def _serialize_multipartform(self, data: Mapping[object, object]) -> dict[str, object]:
        items = self.qs.stringify_items(
            # TODO: type ignore is required as stringify_items is well typed but we can't be
            # well typed without heavy validation.
            data,  # type: ignore
            array_format="brackets",
        )
        serialized: dict[str, object] = {}
        for key, value in items:
            if key in serialized:
                raise ValueError(f"Duplicate key encountered: {key}; This behaviour is not supported")
            serialized[key] = value
        return serialized

    def _process_response(
        self,
        *,
        cast_to: Type[ResponseT],
        options: FinalRequestOptions,
        response: httpx.Response,
    ) -> ResponseT:
        if cast_to is NoneType:
            return cast(ResponseT, None)

        if cast_to == str:
            return cast(ResponseT, response.text)

        origin = get_origin(cast_to) or cast_to

        if inspect.isclass(origin) and issubclass(origin, httpx.Response):
            # Because of the invariance of our ResponseT TypeVar, users can subclass httpx.Response
            # and pass that class to our request functions. We cannot change the variance to be either
            # covariant or contravariant as that makes our usage of ResponseT illegal. We could construct
            # the response class ourselves but that is something that should be supported directly in httpx
            # as it would be easy to incorrectly construct the Response object due to the multitude of arguments.
            if cast_to != httpx.Response:
                raise ValueError(f"Subclasses of httpx.Response cannot be passed to `cast_to`")
            return cast(ResponseT, response)

        # The check here is necessary as we are subverting the the type system
        # with casts as the relationship between TypeVars and Types are very strict
        # which means we must return *exactly* what was input or transform it in a
        # way that retains the TypeVar state. As we cannot do that in this function
        # then we have to resort to using `cast`. At the time of writing, we know this
        # to be safe as we have handled all the types that could be bound to the
        # `ResponseT` TypeVar, however if that TypeVar is ever updated in the future, then
        # this function would become unsafe but a type checker would not report an error.
        if (
            cast_to is not UnknownResponse
            and not origin is list
            and not origin is dict
            and not origin is Union
            and not issubclass(origin, BaseModel)
        ):
            raise RuntimeError(
                f"Invalid state, expected {cast_to} to be a subclass type of {BaseModel}, {dict}, {list} or {Union}."
            )

        # split is required to handle cases where additional information is included
        # in the response, e.g. application/json; charset=utf-8
        content_type, *_ = response.headers.get("content-type").split(";")
        if content_type != "application/json":
            raise ValueError(
                f"Expected Content-Type response header to be `application/json` but received {content_type} instead."
            )

        data = response.json()
        return self._process_response_data(data=data, cast_to=cast_to, response=response)

    def _process_response_data(
        self,
        *,
        data: object,
        cast_to: type[ResponseT],
        response: httpx.Response,
    ) -> ResponseT:
        if data is None:
            return cast(ResponseT, None)

        if cast_to is UnknownResponse:
            return cast(ResponseT, data)

        if inspect.isclass(cast_to) and issubclass(cast_to, ModelBuilderProtocol):
            return cast(ResponseT, cast_to.build(response=response, data=data))

        if self._strict_response_validation:
            return cast(ResponseT, validate_type(type_=cast_to, value=data))

        return cast(ResponseT, construct_type(type_=cast_to, value=data))

    @property
    def qs(self) -> Querystring:
        return Querystring()

    @property
    def custom_auth(self) -> httpx.Auth | None:
        return None

    @property
    def auth_headers(self) -> dict[str, str]:
        return {}

    @property
    def default_headers(self) -> dict[str, str | Omit]:
        return {
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
            **self.platform_headers(),
            **self.auth_headers,
            **self._custom_headers,
        }

    def _validate_headers(self, headers: Headers, custom_headers: Headers) -> None:
        """Validate the given default headers and custom headers.

        Does nothing by default.
        """
        return

    @property
    def user_agent(self) -> str:
        return f"{self.__class__.__name__}/Python {self._version}"

    @property
    def base_url(self) -> URL:
        return self._client.base_url

    @lru_cache(maxsize=None)
    def platform_headers(self) -> Dict[str, str]:
        return {
            "X-Stainless-Lang": "python",
            "X-Stainless-Package-Version": self._version,
            "X-Stainless-OS": str(get_platform()),
            "X-Stainless-Arch": str(get_architecture()),
            "X-Stainless-Runtime": platform.python_implementation(),
            "X-Stainless-Runtime-Version": platform.python_version(),
        }

    def _calculate_retry_timeout(
        self,
        remaining_retries: int,
        options: FinalRequestOptions,
        response_headers: Optional[httpx.Headers] = None,
    ) -> float:
        max_retries = options.get_max_retries(self.max_retries)
        try:
            # About the Retry-After header: https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Retry-After
            #
            # TODO: we may want to handle the case where the header is using the http-date syntax: "Retry-After:
            # <http-date>". See https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Retry-After#syntax for
            # details.
            retry_after = -1 if response_headers is None else int(response_headers.get("retry-after"))
        except Exception:
            retry_after = -1

        # If the API asks us to wait a certain amount of time (and it's a reasonable amount), just do what it says.
        if 0 < retry_after <= 60:
            return retry_after

        initial_retry_delay = 0.5
        max_retry_delay = 2.0
        nb_retries = max_retries - remaining_retries

        # Apply exponential backoff, but not more than the max.
        sleep_seconds = min(initial_retry_delay * pow(nb_retries - 1, 2), max_retry_delay)

        # Apply some jitter, plus-or-minus half a second.
        jitter = random() - 0.5
        timeout = sleep_seconds + jitter
        return timeout if timeout >= 0 else 0

    def _should_retry(self, response: httpx.Response) -> bool:
        # Note: this is not a standard header
        should_retry_header = response.headers.get("x-should-retry")

        # If the server explicitly says whether or not to retry, obey.
        if should_retry_header == "true":
            return True
        if should_retry_header == "false":
            return False

        # Retry on lock timeouts.
        if response.status_code == 409:
            return True

        # Retry on rate limits.
        if response.status_code == 429:
            return True

        # Retry internal errors.
        if response.status_code >= 500:
            return True

        return False

    def _idempotency_key(self) -> str:
        return f"stainless-python-retry-{uuid.uuid4()}"


class SyncAPIClient(BaseClient):
    _client: httpx.Client
    _default_stream_cls: type[Stream[Any]] | None = None

    def __init__(
        self,
        *,
        version: str,
        base_url: str,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout: float | Timeout | None = DEFAULT_TIMEOUT,
        transport: Transport | None = None,
        proxies: ProxiesTypes | None = None,
        limits: Limits | None = DEFAULT_LIMITS,
        verify: bool | str | ssl.SSLContext = True,
        custom_headers: Mapping[str, str] | None = None,
        custom_query: Mapping[str, object] | None = None,
        _strict_response_validation: bool,
    ) -> None:
        limits = limits or DEFAULT_LIMITS
        super().__init__(
            version=version,
            limits=limits,
            timeout=timeout,
            max_retries=max_retries,
            custom_query=custom_query,
            custom_headers=custom_headers,
            _strict_response_validation=_strict_response_validation,
        )
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            proxies=proxies,  # type: ignore
            transport=transport,  # type: ignore
            limits=limits,
            verify=verify,
            headers={"Accept": "application/json"},
        )

    def is_closed(self) -> bool:
        return self._client.is_closed

    def close(self) -> None:
        """Close the underlying HTTPX client.

        The client will *not* be usable after this.
        """
        self._client.close()

    def __enter__(self: _T) -> _T:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    @overload
    def request(
        self,
        cast_to: Type[ResponseT],
        options: FinalRequestOptions,
        remaining_retries: Optional[int] = None,
        *,
        stream: Literal[True],
        stream_cls: Type[_StreamT],
    ) -> _StreamT:
        ...

    @overload
    def request(
        self,
        cast_to: Type[ResponseT],
        options: FinalRequestOptions,
        remaining_retries: Optional[int] = None,
        *,
        stream: Literal[False] = False,
    ) -> ResponseT:
        ...

    @overload
    def request(
        self,
        cast_to: Type[ResponseT],
        options: FinalRequestOptions,
        remaining_retries: Optional[int] = None,
        *,
        stream: bool = False,
        stream_cls: Type[_StreamT] | None = None,
    ) -> ResponseT | _StreamT:
        ...

    def request(
        self,
        cast_to: Type[ResponseT],
        options: FinalRequestOptions,
        remaining_retries: Optional[int] = None,
        *,
        stream: bool = False,
        stream_cls: type[_StreamT] | None = None,
    ) -> ResponseT | _StreamT:
        return self._request(
            cast_to=cast_to,
            options=options,
            stream=stream,
            stream_cls=stream_cls,
            remaining_retries=remaining_retries,
        )

    def _request(
        self,
        *,
        cast_to: Type[ResponseT],
        options: FinalRequestOptions,
        remaining_retries: int | None,
        stream: bool,
        stream_cls: type[_StreamT] | None,
    ) -> ResponseT | _StreamT:
        retries = self._remaining_retries(remaining_retries, options)
        request = self._build_request(options)

        try:
            response = self._client.send(request, auth=self.custom_auth, stream=stream)
            response.raise_for_status()
        except httpx.HTTPStatusError as err:  # thrown on 4xx and 5xx status code
            if retries > 0 and self._should_retry(err.response):
                return self._retry_request(
                    options,
                    cast_to,
                    retries,
                    err.response.headers,
                    stream=stream,
                    stream_cls=stream_cls,
                )

            # If the response is streamed then we need to explicitly read the response
            # to completion before attempting to access the response text.
            err.response.read()
            raise self._make_status_error_from_response(request, err.response) from None
        except httpx.TimeoutException as err:
            if retries > 0:
                return self._retry_request(options, cast_to, retries, stream=stream, stream_cls=stream_cls)
            raise APITimeoutError(request=request) from err
        except Exception as err:
            if retries > 0:
                return self._retry_request(options, cast_to, retries, stream=stream, stream_cls=stream_cls)
            raise APIConnectionError(request=request) from err

        if stream:
            stream_cls = stream_cls or cast("type[_StreamT] | None", self._default_stream_cls)
            if stream_cls is None:
                raise MissingStreamClassError()
            return stream_cls(cast_to=cast_to, response=response, client=self)

        try:
            rsp = self._process_response(cast_to=cast_to, options=options, response=response)
        except pydantic.ValidationError as err:
            raise APIResponseValidationError(request=request, response=response) from err

        return rsp

    def _retry_request(
        self,
        options: FinalRequestOptions,
        cast_to: Type[ResponseT],
        remaining_retries: int,
        response_headers: Optional[httpx.Headers] = None,
        *,
        stream: bool,
        stream_cls: type[_StreamT] | None,
    ) -> ResponseT | _StreamT:
        remaining = remaining_retries - 1
        timeout = self._calculate_retry_timeout(remaining, options, response_headers)

        # In a synchronous context we are blocking the entire thread. Up to the library user to run the client in a
        # different thread if necessary.
        time.sleep(timeout)

        return self._request(
            options=options,
            cast_to=cast_to,
            remaining_retries=remaining,
            stream=stream,
            stream_cls=stream_cls,
        )

    def _request_api_list(
        self,
        model: Type[ModelT],
        page: Type[SyncPageT],
        options: FinalRequestOptions,
    ) -> SyncPageT:
        resp = self.request(page, options, stream=False)
        resp._set_private_attributes(  # pyright: ignore[reportPrivateUsage]
            client=self,
            model=model,
            options=options,
        )
        return resp

    @overload
    def get(
        self,
        path: str,
        *,
        cast_to: Type[ResponseT],
        options: RequestOptions = {},
        stream: Literal[False] = False,
    ) -> ResponseT:
        ...

    @overload
    def get(
        self,
        path: str,
        *,
        cast_to: Type[ResponseT],
        options: RequestOptions = {},
        stream: Literal[True],
        stream_cls: type[_StreamT],
    ) -> _StreamT:
        ...

    @overload
    def get(
        self,
        path: str,
        *,
        cast_to: Type[ResponseT],
        options: RequestOptions = {},
        stream: bool,
        stream_cls: type[_StreamT] | None = None,
    ) -> ResponseT | _StreamT:
        ...

    def get(
        self,
        path: str,
        *,
        cast_to: Type[ResponseT],
        options: RequestOptions = {},
        stream: bool = False,
        stream_cls: type[_StreamT] | None = None,
    ) -> ResponseT | _StreamT:
        opts = FinalRequestOptions.construct(method="get", url=path, **options)
        # cast is required because mypy complains about returning Any even though
        # it understands the type variables
        return cast(ResponseT, self.request(cast_to, opts, stream=stream, stream_cls=stream_cls))

    @overload
    def post(
        self,
        path: str,
        *,
        cast_to: Type[ResponseT],
        body: Body | None = None,
        options: RequestOptions = {},
        files: RequestFiles | None = None,
        stream: Literal[False] = False,
    ) -> ResponseT:
        ...

    @overload
    def post(
        self,
        path: str,
        *,
        cast_to: Type[ResponseT],
        body: Body | None = None,
        options: RequestOptions = {},
        files: RequestFiles | None = None,
        stream: Literal[True],
        stream_cls: type[_StreamT],
    ) -> _StreamT:
        ...

    @overload
    def post(
        self,
        path: str,
        *,
        cast_to: Type[ResponseT],
        body: Body | None = None,
        options: RequestOptions = {},
        files: RequestFiles | None = None,
        stream: bool,
        stream_cls: type[_StreamT] | None = None,
    ) -> ResponseT | _StreamT:
        ...

    def post(
        self,
        path: str,
        *,
        cast_to: Type[ResponseT],
        body: Body | None = None,
        options: RequestOptions = {},
        files: RequestFiles | None = None,
        stream: bool = False,
        stream_cls: type[_StreamT] | None = None,
    ) -> ResponseT | _StreamT:
        opts = FinalRequestOptions.construct(method="post", url=path, json_data=body, files=files, **options)
        return cast(ResponseT, self.request(cast_to, opts, stream=stream, stream_cls=stream_cls))

    def patch(
        self,
        path: str,
        *,
        cast_to: Type[ResponseT],
        body: Body | None = None,
        options: RequestOptions = {},
    ) -> ResponseT:
        opts = FinalRequestOptions.construct(method="patch", url=path, json_data=body, **options)
        return self.request(cast_to, opts)

    def put(
        self,
        path: str,
        *,
        cast_to: Type[ResponseT],
        body: Body | None = None,
        files: RequestFiles | None = None,
        options: RequestOptions = {},
    ) -> ResponseT:
        opts = FinalRequestOptions.construct(method="put", url=path, json_data=body, files=files, **options)
        return self.request(cast_to, opts)

    def delete(
        self,
        path: str,
        *,
        cast_to: Type[ResponseT],
        body: Body | None = None,
        options: RequestOptions = {},
    ) -> ResponseT:
        opts = FinalRequestOptions.construct(method="delete", url=path, json_data=body, **options)
        return self.request(cast_to, opts)

    def get_api_list(
        self,
        path: str,
        *,
        model: Type[ModelT],
        page: Type[SyncPageT],
        body: Body | None = None,
        options: RequestOptions = {},
        method: str = "get",
    ) -> SyncPageT:
        opts = FinalRequestOptions.construct(method=method, url=path, json_data=body, **options)
        return self._request_api_list(model, page, opts)


class AsyncAPIClient(BaseClient):
    _client: httpx.AsyncClient
    _default_stream_cls: type[AsyncStream[Any]] | None = None

    def __init__(
        self,
        *,
        version: str,
        base_url: str,
        _strict_response_validation: bool,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout: float | Timeout | None = DEFAULT_TIMEOUT,
        transport: Transport | None = None,
        proxies: ProxiesTypes | None = None,
        limits: Limits | None = DEFAULT_LIMITS,
        verify: bool | str | ssl.SSLContext = True,
        custom_headers: Mapping[str, str] | None = None,
        custom_query: Mapping[str, object] | None = None,
    ) -> None:
        limits = limits or DEFAULT_LIMITS
        super().__init__(
            version=version,
            limits=limits,
            timeout=timeout,
            max_retries=max_retries,
            custom_query=custom_query,
            custom_headers=custom_headers,
            _strict_response_validation=_strict_response_validation,
        )
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            proxies=proxies,  # type: ignore
            transport=transport,  # type: ignore
            limits=limits,
            verify=verify,
            headers={"Accept": "application/json"},
        )

    def is_closed(self) -> bool:
        return self._client.is_closed

    async def close(self) -> None:
        """Close the underlying HTTPX client.

        The client will *not* be usable after this.
        """
        await self._client.aclose()

    async def __aenter__(self: _T) -> _T:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.close()

    @overload
    async def request(
        self,
        cast_to: Type[ResponseT],
        options: FinalRequestOptions,
        *,
        stream: Literal[False] = False,
        remaining_retries: Optional[int] = None,
    ) -> ResponseT:
        ...

    @overload
    async def request(
        self,
        cast_to: Type[ResponseT],
        options: FinalRequestOptions,
        *,
        stream: Literal[True],
        stream_cls: type[_AsyncStreamT],
        remaining_retries: Optional[int] = None,
    ) -> _AsyncStreamT:
        ...

    @overload
    async def request(
        self,
        cast_to: Type[ResponseT],
        options: FinalRequestOptions,
        *,
        stream: bool,
        stream_cls: type[_AsyncStreamT] | None = None,
        remaining_retries: Optional[int] = None,
    ) -> ResponseT | _AsyncStreamT:
        ...

    async def request(
        self,
        cast_to: Type[ResponseT],
        options: FinalRequestOptions,
        *,
        stream: bool = False,
        stream_cls: type[_AsyncStreamT] | None = None,
        remaining_retries: Optional[int] = None,
    ) -> ResponseT | _AsyncStreamT:
        return await self._request(
            cast_to=cast_to,
            options=options,
            stream=stream,
            stream_cls=stream_cls,
            remaining_retries=remaining_retries,
        )

    async def _request(
        self,
        cast_to: Type[ResponseT],
        options: FinalRequestOptions,
        *,
        stream: bool,
        stream_cls: type[_AsyncStreamT] | None,
        remaining_retries: int | None,
    ) -> ResponseT | _AsyncStreamT:
        retries = self._remaining_retries(remaining_retries, options)
        request = self._build_request(options)

        try:
            response = await self._client.send(request, auth=self.custom_auth, stream=stream)
            response.raise_for_status()
        except httpx.HTTPStatusError as err:  # thrown on 4xx and 5xx status code
            if retries > 0 and self._should_retry(err.response):
                return await self._retry_request(
                    options,
                    cast_to,
                    retries,
                    err.response.headers,
                    stream=stream,
                    stream_cls=stream_cls,
                )

            # If the response is streamed then we need to explicitly read the response
            # to completion before attempting to access the response text.
            await err.response.aread()
            raise self._make_status_error_from_response(request, err.response) from None
        except httpx.ConnectTimeout as err:
            if retries > 0:
                return await self._retry_request(options, cast_to, retries, stream=stream, stream_cls=stream_cls)
            raise APITimeoutError(request=request) from err
        except httpx.ReadTimeout as err:
            # We explicitly do not retry on ReadTimeout errors as this means
            # that the server processing the request has taken 60 seconds
            # (our default timeout). This likely indicates that something
            # is not working as expected on the server side.
            raise
        except httpx.TimeoutException as err:
            if retries > 0:
                return await self._retry_request(options, cast_to, retries, stream=stream, stream_cls=stream_cls)
            raise APITimeoutError(request=request) from err
        except Exception as err:
            if retries > 0:
                return await self._retry_request(options, cast_to, retries, stream=stream, stream_cls=stream_cls)
            raise APIConnectionError(request=request) from err

        if stream:
            stream_cls = stream_cls or cast("type[_AsyncStreamT] | None", self._default_stream_cls)
            if stream_cls is None:
                raise MissingStreamClassError()
            return stream_cls(cast_to=cast_to, response=response, client=self)

        try:
            rsp = self._process_response(cast_to=cast_to, options=options, response=response)
        except pydantic.ValidationError as err:
            raise APIResponseValidationError(request=request, response=response) from err

        return rsp

    async def _retry_request(
        self,
        options: FinalRequestOptions,
        cast_to: Type[ResponseT],
        remaining_retries: int,
        response_headers: Optional[httpx.Headers] = None,
        *,
        stream: bool,
        stream_cls: type[_AsyncStreamT] | None,
    ) -> ResponseT | _AsyncStreamT:
        remaining = remaining_retries - 1
        timeout = self._calculate_retry_timeout(remaining, options, response_headers)

        await anyio.sleep(timeout)

        return await self._request(
            options=options,
            cast_to=cast_to,
            remaining_retries=remaining,
            stream=stream,
            stream_cls=stream_cls,
        )

    def _request_api_list(
        self,
        model: Type[ModelT],
        page: Type[AsyncPageT],
        options: FinalRequestOptions,
    ) -> AsyncPaginator[ModelT, AsyncPageT]:
        return AsyncPaginator(client=self, options=options, page_cls=page, model=model)

    @overload
    async def get(
        self,
        path: str,
        *,
        cast_to: Type[ResponseT],
        options: RequestOptions = {},
        stream: Literal[False] = False,
    ) -> ResponseT:
        ...

    @overload
    async def get(
        self,
        path: str,
        *,
        cast_to: Type[ResponseT],
        options: RequestOptions = {},
        stream: Literal[True],
        stream_cls: type[_AsyncStreamT],
    ) -> _AsyncStreamT:
        ...

    @overload
    async def get(
        self,
        path: str,
        *,
        cast_to: Type[ResponseT],
        options: RequestOptions = {},
        stream: bool,
        stream_cls: type[_AsyncStreamT] | None = None,
    ) -> ResponseT | _AsyncStreamT:
        ...

    async def get(
        self,
        path: str,
        *,
        cast_to: Type[ResponseT],
        options: RequestOptions = {},
        stream: bool = False,
        stream_cls: type[_AsyncStreamT] | None = None,
    ) -> ResponseT | _AsyncStreamT:
        opts = FinalRequestOptions.construct(method="get", url=path, **options)
        return await self.request(cast_to, opts, stream=stream, stream_cls=stream_cls)

    @overload
    async def post(
        self,
        path: str,
        *,
        cast_to: Type[ResponseT],
        body: Body | None = None,
        files: RequestFiles | None = None,
        options: RequestOptions = {},
        stream: Literal[False] = False,
    ) -> ResponseT:
        ...

    @overload
    async def post(
        self,
        path: str,
        *,
        cast_to: Type[ResponseT],
        body: Body | None = None,
        files: RequestFiles | None = None,
        options: RequestOptions = {},
        stream: Literal[True],
        stream_cls: type[_AsyncStreamT],
    ) -> _AsyncStreamT:
        ...

    @overload
    async def post(
        self,
        path: str,
        *,
        cast_to: Type[ResponseT],
        body: Body | None = None,
        files: RequestFiles | None = None,
        options: RequestOptions = {},
        stream: bool,
        stream_cls: type[_AsyncStreamT] | None = None,
    ) -> ResponseT | _AsyncStreamT:
        ...

    async def post(
        self,
        path: str,
        *,
        cast_to: Type[ResponseT],
        body: Body | None = None,
        files: RequestFiles | None = None,
        options: RequestOptions = {},
        stream: bool = False,
        stream_cls: type[_AsyncStreamT] | None = None,
    ) -> ResponseT | _AsyncStreamT:
        opts = FinalRequestOptions.construct(method="post", url=path, json_data=body, files=files, **options)
        return await self.request(cast_to, opts, stream=stream, stream_cls=stream_cls)

    async def patch(
        self,
        path: str,
        *,
        cast_to: Type[ResponseT],
        body: Body | None = None,
        options: RequestOptions = {},
    ) -> ResponseT:
        opts = FinalRequestOptions.construct(method="patch", url=path, json_data=body, **options)
        return await self.request(cast_to, opts)

    async def put(
        self,
        path: str,
        *,
        cast_to: Type[ResponseT],
        body: Body | None = None,
        files: RequestFiles | None = None,
        options: RequestOptions = {},
    ) -> ResponseT:
        opts = FinalRequestOptions.construct(method="put", url=path, json_data=body, files=files, **options)
        return await self.request(cast_to, opts)

    async def delete(
        self,
        path: str,
        *,
        cast_to: Type[ResponseT],
        body: Body | None = None,
        options: RequestOptions = {},
    ) -> ResponseT:
        opts = FinalRequestOptions.construct(method="delete", url=path, json_data=body, **options)
        return await self.request(cast_to, opts)

    def get_api_list(
        self,
        path: str,
        *,
        # TODO: support paginating `str`
        model: Type[ModelT],
        page: Type[AsyncPageT],
        body: Body | None = None,
        options: RequestOptions = {},
        method: str = "get",
    ) -> AsyncPaginator[ModelT, AsyncPageT]:
        opts = FinalRequestOptions.construct(method=method, url=path, json_data=body, **options)
        return self._request_api_list(model, page, opts)


def make_request_options(
    *,
    query: Query | None = None,
    extra_headers: Headers | None = None,
    extra_query: Query | None = None,
    extra_body: Body | None = None,
    idempotency_key: str | None = None,
    timeout: float | None | NotGiven = NOT_GIVEN,
) -> RequestOptions:
    """Create a dict of type RequestOptions without keys of NotGiven values."""
    options: RequestOptions = {}
    if extra_headers is not None:
        options["headers"] = extra_headers

    if extra_body is not None:
        options["extra_json"] = cast(AnyMapping, extra_body)

    if query is not None:
        options["params"] = query

    if extra_query is not None:
        options["params"] = {**options.get("params", {}), **extra_query}

    if not isinstance(timeout, NotGiven):
        options["timeout"] = timeout

    if idempotency_key is not None:
        options["idempotency_key"] = idempotency_key

    return options


class OtherPlatform:
    def __init__(self, name: str) -> None:
        self.name = name

    def __str__(self) -> str:
        return f"Other:{self.name}"


Platform = Union[
    OtherPlatform,
    Literal[
        "MacOS",
        "Linux",
        "Windows",
        "FreeBSD",
        "OpenBSD",
        "iOS",
        "Android",
        "Unknown",
    ],
]


def get_platform() -> Platform:
    system = platform.system().lower()
    platform_name = platform.platform().lower()
    if "iphone" in platform_name or "ipad" in platform_name:
        # Tested using Python3IDE on an iPhone 11 and Pythonista on an iPad 7
        # system is Darwin and platform_name is a string like:
        # - Darwin-21.6.0-iPhone12,1-64bit
        # - Darwin-21.6.0-iPad7,11-64bit
        return "iOS"

    if system == "darwin":
        return "MacOS"

    if system == "windows":
        return "Windows"

    if "android" in platform_name:
        # Tested using Pydroid 3
        # system is Linux and platform_name is a string like 'Linux-5.10.81-android12-9-00001-geba40aecb3b7-ab8534902-aarch64-with-libc'
        return "Android"

    if system == "linux":
        # https://distro.readthedocs.io/en/latest/#distro.id
        distro_id = distro.id()
        if distro_id == "freebsd":
            return "FreeBSD"

        if distro_id == "openbsd":
            return "OpenBSD"

        return "Linux"

    if platform_name:
        return OtherPlatform(platform_name)

    return "Unknown"


class OtherArch:
    def __init__(self, name: str) -> None:
        self.name = name

    def __str__(self) -> str:
        return f"other:{self.name}"


Arch = Union[OtherArch, Literal["x32", "x64", "arm", "arm64", "unknown"]]


def get_architecture() -> Arch:
    python_bitness, _ = platform.architecture()
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "arm64"

    # TODO: untested
    if machine == "arm":
        return "arm"

    if machine == "x86_64":
        return "x64"

    # TODO: untested
    if python_bitness == "32bit":
        return "x32"

    if machine:
        return OtherArch(machine)

    return "unknown"


def _merge_mappings(
    obj1: Mapping[_T_co, Union[_T, Omit]],
    obj2: Mapping[_T_co, Union[_T, Omit]],
) -> Dict[_T_co, _T]:
    """Merge two mappings of the same type, removing any values that are instances of `Omit`.

    In cases with duplicate keys the second mapping takes precedence.
    """
    merged = {**obj1, **obj2}
    return {key: value for key, value in merged.items() if not isinstance(value, Omit)}
