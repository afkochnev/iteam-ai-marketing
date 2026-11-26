from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Self

import httpx

# An approved article may contain 100,000 characters. WordPress can escape each
# non-BMP character as 12 bytes, and a slug lookup can return up to two posts.
MAX_RESPONSE_BYTES = 3_000_000
POST_FIELDS = "id,type,status,slug,author,title.raw,content.raw,featured_media,link"
IMAGE_FIELDS = "id,type,slug,mime_type,author,post,alt_text,source_url"


class WordPressError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class WordPressTarget:
    url: str
    author_id: int

    @classmethod
    def from_config(cls, url: str | None, author_id: int | None) -> Self | None:
        if not url or not author_id:
            return None
        try:
            parsed = httpx.URL(url)
        except httpx.InvalidURL:
            return None
        if not parsed.host or parsed.userinfo or parsed.query or parsed.fragment:
            return None
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.host in {"localhost", "127.0.0.1", "::1"}
        ):
            return None
        normalized = str(parsed).rstrip("/")
        return cls(normalized, author_id) if len(normalized) <= 512 else None

    def contains_url(self, url: object) -> bool:
        if not isinstance(url, str) or len(url) > 2048:
            return False
        try:
            candidate = httpx.URL(url)
        except httpx.InvalidURL:
            return False
        target = httpx.URL(self.url)
        target_path = target.path.rstrip("/")
        return (
            candidate.is_absolute_url
            and not candidate.userinfo
            and (candidate.scheme, candidate.host, candidate.port)
            == (target.scheme, target.host, target.port)
            and (not target_path or candidate.path.startswith(f"{target_path}/"))
        )


@dataclass(frozen=True)
class WordPressArticle:
    slug: str
    title: str
    html: str


@dataclass(frozen=True)
class WordPressImage:
    slug: str
    sha256: str
    size: int
    alt: str


@dataclass(frozen=True)
class WordPressPost:
    id: int
    url: str | None = None


def _raw(field: object) -> object:
    return field.get("raw") if isinstance(field, dict) else None


def _positive_id(value: object) -> int:
    if type(value) is not int or not 0 < value <= 2**63 - 1:
        raise WordPressError("WORDPRESS_RESPONSE_MISMATCH")
    return value


class WordPressStudioClient:
    """Calls only the configured staging WordPress REST API."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        target: WordPressTarget,
        username: str,
        application_password: str,
    ) -> None:
        self.client = client
        self.target = target
        self.auth = httpx.BasicAuth(username, application_password)
        self.write_attempted = False

    async def verify_actor(self) -> None:
        result = await self._request("GET", "/users/me", fields="id")
        if not isinstance(result, dict) or _positive_id(result.get("id")) != self.target.author_id:
            raise WordPressError("WORDPRESS_ACTOR_MISMATCH")

    async def create_draft(self, article: WordPressArticle) -> WordPressPost:
        result = await self._request(
            "POST",
            "/posts",
            fields=POST_FIELDS,
            json={
                "slug": article.slug,
                "title": article.title,
                "content": article.html,
                "status": "draft",
            },
        )
        return self._post(result, article, "draft", media_id=0)

    async def find_draft(self, article: WordPressArticle) -> WordPressPost | None:
        candidate = await self._find_one("/posts", article.slug, "draft,publish", POST_FIELDS)
        return self._post(candidate, article, "draft", media_id=0) if candidate else None

    async def read_draft(self, article: WordPressArticle, post_id: int) -> None:
        result = await self._request("GET", f"/posts/{post_id}", fields=POST_FIELDS)
        self._post(result, article, "draft", id=post_id, media_id=0)

    async def upload_image(self, image: WordPressImage, post_id: int, content: bytes) -> int:
        if len(content) != image.size or sha256(content).hexdigest() != image.sha256:
            raise WordPressError("WORDPRESS_LOCAL_MEDIA_MISMATCH")
        result = await self._request(
            "POST",
            "/media",
            fields=IMAGE_FIELDS,
            data={"slug": image.slug, "post": str(post_id), "alt_text": image.alt},
            files={"file": (f"{image.slug}.webp", content, "image/webp")},
        )
        return await self._image(result, image, post_id)

    async def find_image(self, image: WordPressImage, post_id: int) -> int | None:
        candidate = await self._find_one("/media", image.slug, "inherit", IMAGE_FIELDS)
        return await self._image(candidate, image, post_id) if candidate else None

    async def verify_image(self, image: WordPressImage, post_id: int, media_id: int) -> None:
        result = await self._request("GET", f"/media/{media_id}", fields=IMAGE_FIELDS)
        if await self._image(result, image, post_id) != media_id:
            raise WordPressError("WORDPRESS_RESPONSE_MISMATCH")

    async def publish(
        self, article: WordPressArticle, post_id: int, media_id: int
    ) -> WordPressPost:
        result = await self._request(
            "POST",
            f"/posts/{post_id}",
            fields=POST_FIELDS,
            json={
                "status": "publish",
                "slug": article.slug,
                "title": article.title,
                "content": article.html,
                "featured_media": media_id,
            },
        )
        return self._post(result, article, "publish", id=post_id, media_id=media_id)

    async def find_publication(
        self, article: WordPressArticle, post_id: int, media_id: int
    ) -> WordPressPost | None:
        result = await self._request("GET", f"/posts/{post_id}", fields=POST_FIELDS)
        if isinstance(result, dict) and result.get("status") == "draft":
            self._post(result, article, "draft", id=post_id, media_id=0)
            return None
        return self._post(result, article, "publish", id=post_id, media_id=media_id)

    async def _find_one(
        self, endpoint: str, slug: str, status: str, fields: str
    ) -> dict[str, Any] | None:
        result = await self._request(
            "GET",
            endpoint,
            fields=fields,
            params={"slug": slug, "status": status, "per_page": 2},
        )
        if not isinstance(result, list) or len(result) > 1:
            raise WordPressError("WORDPRESS_LOOKUP_AMBIGUOUS")
        if not result:
            return None
        candidate = result[0]
        if not isinstance(candidate, dict):
            raise WordPressError("WORDPRESS_RESPONSE_MISMATCH")
        return candidate

    def _post(
        self,
        result: object,
        article: WordPressArticle,
        status: str,
        *,
        media_id: int,
        id: int | None = None,
    ) -> WordPressPost:
        if not isinstance(result, dict):
            raise WordPressError("WORDPRESS_RESPONSE_MISMATCH")
        remote_id = _positive_id(result.get("id"))
        if (
            (id is not None and remote_id != id)
            or result.get("type") != "post"
            or result.get("status") != status
            or result.get("slug") != article.slug
            or result.get("author") != self.target.author_id
            or _raw(result.get("title")) != article.title
            or _raw(result.get("content")) != article.html
            or result.get("featured_media") != media_id
        ):
            raise WordPressError("WORDPRESS_RESPONSE_MISMATCH")
        url = result.get("link")
        if status == "publish":
            if not isinstance(url, str) or not self.target.contains_url(url):
                raise WordPressError("WORDPRESS_URL_MISMATCH")
            return WordPressPost(remote_id, url)
        return WordPressPost(remote_id)

    async def _image(self, result: object, image: WordPressImage, post_id: int) -> int:
        if not isinstance(result, dict) or (
            result.get("type") != "attachment"
            or result.get("slug") != image.slug
            or result.get("mime_type") != "image/webp"
            or result.get("author") != self.target.author_id
            or result.get("post") != post_id
            or result.get("alt_text") != image.alt
        ):
            raise WordPressError("WORDPRESS_RESPONSE_MISMATCH")
        media_id = _positive_id(result.get("id"))
        url = result.get("source_url")
        if not isinstance(url, str) or not self.target.contains_url(url):
            raise WordPressError("WORDPRESS_URL_MISMATCH")
        content = await self._body("GET", url, limit=image.size, auth=None)
        if len(content) != image.size or sha256(content).hexdigest() != image.sha256:
            raise WordPressError("WORDPRESS_REMOTE_MEDIA_MISMATCH")
        return media_id

    async def _request(
        self,
        method: str,
        path: str,
        *,
        fields: str,
        params: dict[str, str | int] | None = None,
        **kwargs: Any,
    ) -> Any:
        if method == "POST":
            self.write_attempted = True
        body = await self._body(
            method,
            f"{self.target.url}/wp-json/wp/v2{path}",
            limit=MAX_RESPONSE_BYTES,
            auth=self.auth,
            params={"context": "edit", "_fields": fields, **(params or {})},
            **kwargs,
        )
        try:
            return httpx.Response(200, content=body).json()
        except ValueError:
            raise WordPressError("WORDPRESS_RESPONSE_INVALID") from None

    async def _body(self, method: str, url: str, limit: int, **kwargs: Any) -> bytes:
        try:
            async with self.client.stream(method, url, follow_redirects=False, **kwargs) as res:
                if not res.is_success:
                    raise WordPressError("WORDPRESS_HTTP_ERROR")
                content = bytearray()
                async for data in res.aiter_bytes():
                    if len(content) + len(data) > limit:
                        raise WordPressError("WORDPRESS_RESPONSE_TOO_LARGE")
                    content.extend(data)
                return bytes(content)
        except httpx.HTTPError:
            raise WordPressError("WORDPRESS_CONNECTION_ERROR") from None
