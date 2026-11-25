import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { StudioWorkspace } from "@/components/studio/workspace";
import { ApiError } from "@/lib/api";
import type { StudioAsset, StudioVersion } from "@/lib/studio";

const api = vi.hoisted(() => ({
  assets: vi.fn(),
  versions: vi.fn(),
  version: vi.fn(),
  saveArticle: vi.fn(),
  uploadIllustration: vi.fn(),
  approve: vi.fn(),
  media: vi.fn(),
  websitePackage: vi.fn(),
  websiteStatus: vi.fn(),
  createWebsiteDraft: vi.fn(),
  publishWebsiteDraft: vi.fn(),
  websiteDraftUrl: vi.fn(),
  websitePublicUrl: vi.fn(),
  generate: vi.fn(),
  generationJob: vi.fn(),
}));
vi.mock("@/lib/studio", () => ({ studioApi: api }));

const campaignId = "campaign-1";
const timestamp = "2026-11-24T12:00:00Z";
const approval = { approved_at: timestamp, approved_by: "editor-1" };
const original = { sha256: "a".repeat(64), width: 1200, height: 800, mime_type: "image/png", size_bytes: 749 };

function illustration(overrides: Partial<Extract<StudioVersion, { kind: "ILLUSTRATION" }>> = {}): Extract<StudioVersion, { kind: "ILLUSTRATION" }> {
  return {
    id: "visual-1",
    asset_id: "visual-asset",
    kind: "ILLUSTRATION",
    version_number: 1,
    supersedes_version_id: null,
    source_version_id: null,
    illustration_version_id: null,
    snapshot_hash: "b".repeat(64),
    created_by: "editor-1",
    created_at: timestamp,
    approval: null,
    payload: {
      title: "Иллюстрация для статьи",
      brief: "Команда у доски",
      brand_profile: "Нейтральные цвета",
      alt_text: "Команда у доски с диаграммой",
      usage_rights: "Собственное изображение",
      caption: "Обсуждение плана",
      original,
      variants: [{ ...original, name: "square", width: 800 }],
      provenance: { source: "UPLOAD" },
    },
    ...overrides,
  };
}

function article(overrides: Partial<Extract<StudioVersion, { kind: "ARTICLE" }>> = {}): Extract<StudioVersion, { kind: "ARTICLE" }> {
  return {
    id: "article-1",
    asset_id: "article-asset",
    kind: "ARTICLE",
    version_number: 1,
    supersedes_version_id: null,
    source_version_id: null,
    illustration_version_id: "visual-1",
    snapshot_hash: "c".repeat(64),
    created_by: "editor-1",
    created_at: timestamp,
    approval: null,
    payload: { title: "Стратегическая сессия", body: "Текст статьи" },
    ...overrides,
  };
}

function page<T>(items: T[]) {
  return { items, total: items.length, limit: 50, offset: 0 };
}

function library() {
  return within(screen.getByRole("region", { name: "Материалы студии" }));
}

function fillIllustration() {
  const draft = illustration().payload;
  fireEvent.change(screen.getByLabelText("Название иллюстрации"), { target: { value: draft.title } });
  fireEvent.change(screen.getByLabelText("Бриф иллюстрации"), { target: { value: draft.brief } });
  fireEvent.change(screen.getByLabelText("Профиль бренда"), { target: { value: draft.brand_profile } });
  fireEvent.change(screen.getByLabelText("Альтернативный текст"), { target: { value: draft.alt_text } });
  fireEvent.change(screen.getByLabelText("Права использования"), { target: { value: draft.usage_rights } });
}

let versions: StudioVersion[];

function storeVersion(version: StudioVersion) {
  versions = [version, ...versions.filter((candidate) => candidate.id !== version.id)];
  return version;
}

describe("campaign content studio", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    sessionStorage.clear();
    versions = [];
    class PreviewURL extends URL {
      static createObjectURL = vi.fn(() => "blob:studio-preview");
      static revokeObjectURL = vi.fn();
    }
    vi.stubGlobal("URL", PreviewURL);

    api.assets.mockImplementation(async (_campaign: string, _offset: number, _signal: AbortSignal, kind?: string) => {
      const grouped = new Map<string, StudioAsset>();
      for (const version of versions) {
        if (kind && version.kind !== kind) continue;
        if ((grouped.get(version.asset_id)?.latest_version.version_number ?? 0) >= version.version_number) continue;
        grouped.set(version.asset_id, { id: version.asset_id, kind: version.kind, latest_version: version });
      }
      return page([...grouped.values()]);
    });
    api.versions.mockImplementation(async (_campaign: string, assetId: string, offset: number, _signal: AbortSignal, limit = 50) => {
      const history = versions.filter((version) => version.asset_id === assetId)
        .sort((left, right) => right.version_number - left.version_number);
      return { items: history.slice(offset, offset + limit), total: history.length, limit, offset };
    });
    api.version.mockImplementation(async (_campaign: string, id: string) => versions.find((candidate) => candidate.id === id));
    api.uploadIllustration.mockImplementation(async () => storeVersion(illustration()));
    api.saveArticle.mockImplementation(async () => storeVersion(article()));
    api.approve.mockImplementation(async (_campaign: string, id: string) => {
      const version = versions.find((candidate) => candidate.id === id);
      if (!version) throw new Error("Нет версии");
      return storeVersion({ ...version, approval });
    });
    api.media.mockResolvedValue(new Blob(["preview"], { type: "image/png" }));
    api.websiteStatus.mockResolvedValue({ enabled: false, page: null });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("downloads the selected historical approved article and allows retry after an expired session", async () => {
    const approved = storeVersion(article({ approval }));
    const draft = storeVersion(article({ id: "article-2", version_number: 2, supersedes_version_id: approved.id }));
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    api.websitePackage
      .mockRejectedValueOnce(new ApiError("Требуется вход", 401, "AUTHENTICATION_REQUIRED"))
      .mockResolvedValueOnce(new Blob(["website package"], { type: "application/zip" }));

    render(<StudioWorkspace campaignId={campaignId} />);
    fireEvent.click(await library().findByRole("button", { name: draft.payload.title }));
    expect(screen.queryByRole("button", { name: "Скачать пакет для сайта" })).not.toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Смотреть v1" }));
    fireEvent.click(screen.getByRole("button", { name: "Скачать пакет для сайта" }));
    expect(await screen.findByRole("link", { name: "Войти в новой вкладке" })).toHaveAttribute("href", "/login");
    expect(click).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Скачать пакет для сайта" }));
    await waitFor(() => expect(click).toHaveBeenCalledTimes(1));
    expect(api.websitePackage.mock.calls.map((args) => args.slice(0, 2))).toEqual([
      [campaignId, approved.id], [campaignId, approved.id],
    ]);
    expect(click.mock.instances[0]).toHaveAttribute("download", "article-article-1.zip");
  });

  it("creates a separate test CMS draft before an explicit publish action", async () => {
    const approved = storeVersion(article({ approval }));
    const draft = {
      id: "website-draft", article_version_id: approved.id, illustration_version_id: "visual-1",
      created_at: timestamp, published_at: null,
    };
    api.websiteStatus.mockResolvedValue({ enabled: true, page: null });
    api.createWebsiteDraft.mockResolvedValue(draft);
    api.publishWebsiteDraft.mockResolvedValue({ ...draft, published_at: timestamp });
    api.websiteDraftUrl.mockReturnValue("http://localhost:8000/draft-preview");
    api.websitePublicUrl.mockReturnValue("http://localhost:8000/test-publication");

    render(<StudioWorkspace campaignId={campaignId} />);
    fireEvent.click(await library().findByRole("button", { name: approved.payload.title }));
    const cms = within(await screen.findByRole("region", { name: "Тестовая CMS" }));
    fireEvent.change(cms.getByLabelText("SEO-заголовок"), { target: { value: "Стратегическая сессия iTeam" } });
    fireEvent.click(cms.getByRole("button", { name: "Создать черновик в тестовой CMS" }));
    expect(await cms.findByRole("link", { name: "Посмотреть черновик" })).toHaveAttribute("href", "http://localhost:8000/draft-preview");
    expect(api.createWebsiteDraft).toHaveBeenCalledWith(campaignId, approved.id, { seo_title: "Стратегическая сессия iTeam", seo_description: "" });
    expect(api.publishWebsiteDraft).not.toHaveBeenCalled();

    fireEvent.click(cms.getByRole("button", { name: "Опубликовать на тестовом сайте" }));
    expect(await cms.findByRole("link", { name: "Открыть тестовую публикацию" })).toHaveAttribute("href", "http://localhost:8000/test-publication");
    expect(api.publishWebsiteDraft).toHaveBeenCalledWith(campaignId, approved.id, draft.id);
  });

  it("opens a draft created by another editor after a website SEO conflict", async () => {
    const approved = storeVersion(article({ approval }));
    const draft = {
      id: "existing-website-draft", article_version_id: approved.id, illustration_version_id: null,
      created_at: timestamp, published_at: null,
    };
    const attemptedTitle = "SEO-заголовок второго редактора";
    api.websiteStatus
      .mockResolvedValueOnce({ enabled: true, page: null })
      .mockRejectedValueOnce(new Error("Временная ошибка проверки"))
      .mockResolvedValueOnce({ enabled: true, page: draft });
    api.createWebsiteDraft.mockRejectedValueOnce(
      new ApiError("Для версии уже есть черновик с другими SEO-полями.", 409, "WEBSITE_DRAFT_ALREADY_EXISTS"),
    );
    api.websiteDraftUrl.mockReturnValue("http://localhost:8000/existing-draft-preview");

    render(<StudioWorkspace campaignId={campaignId} />);
    fireEvent.click(await library().findByRole("button", { name: approved.payload.title }));
    const cms = within(await screen.findByRole("region", { name: "Тестовая CMS" }));
    fireEvent.change(cms.getByLabelText("SEO-заголовок"), { target: { value: attemptedTitle } });
    fireEvent.click(cms.getByRole("button", { name: "Создать черновик в тестовой CMS" }));

    const openDraft = "Открыть существующий черновик";
    fireEvent.click(await cms.findByRole("button", { name: openDraft }));
    expect(await cms.findByText("Не удалось загрузить существующий черновик.")).toBeInTheDocument();
    expect(cms.getByLabelText("SEO-заголовок")).toHaveValue(attemptedTitle);
    fireEvent.click(cms.getByRole("button", { name: openDraft }));

    expect(await cms.findByRole("link", { name: "Посмотреть черновик" })).toHaveAttribute(
      "href", "http://localhost:8000/existing-draft-preview",
    );
    expect(api.websiteDraftUrl).toHaveBeenCalledWith(campaignId, approved.id, draft.id);
    expect(api.websiteStatus).toHaveBeenCalledTimes(3);
    expect(api.createWebsiteDraft).toHaveBeenCalledTimes(1);
    expect(api.publishWebsiteDraft).not.toHaveBeenCalled();
  });

  it("uploads and approves an illustration, binds its exact version to an article, and approves the article", async () => {
    render(<StudioWorkspace campaignId={campaignId} />);
    fireEvent.click(screen.getByRole("button", { name: "Новая иллюстрация" }));
    fillIllustration();
    const file = new File(["preview"], "visual.png", { type: "image/png" });
    const uploadInput = screen.getByLabelText<HTMLInputElement>("Файл изображения");
    fireEvent.change(uploadInput, { target: { files: [file] } });
    fireEvent.submit(uploadInput.form!);

    expect(await screen.findByRole("heading", { name: "Иллюстрация для статьи · v1" })).toBeInTheDocument();
    expect(api.uploadIllustration).toHaveBeenCalledWith(campaignId, expect.objectContaining({ usage_rights: "Собственное изображение" }), file, undefined);
    expect(await screen.findByRole("img", { name: "Команда у доски с диаграммой" })).toBeInTheDocument();
    expect(screen.getByText("Загружено редактором")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Утвердить версию v1" }));
    await waitFor(() => expect(screen.getByText("Версия v1 утверждена.")).toBeInTheDocument());
    expect(api.approve).toHaveBeenCalledWith(campaignId, "visual-1");

    // A newer draft must not replace the approved illustration version in the picker.
    storeVersion(illustration({ id: "visual-2", version_number: 2, supersedes_version_id: "visual-1" }));
    fireEvent.click(screen.getByRole("button", { name: "Новая статья" }));
    fireEvent.change(screen.getByLabelText("Название статьи"), { target: { value: "Стратегическая сессия" } });
    fireEvent.change(screen.getByLabelText("Текст статьи"), { target: { value: "Текст статьи" } });
    fireEvent.click(screen.getByRole("button", { name: "Выбрать · версия иллюстрации" }));
    fireEvent.click(await screen.findByRole("button", { name: "Иллюстрация для статьи · иллюстрация" }));
    const exact = await screen.findByRole("button", { name: "Выбрать v1 · утверждена" });
    expect(screen.queryByRole("button", { name: "Выбрать v2 · черновик" })).not.toBeInTheDocument();
    fireEvent.click(exact);
    fireEvent.click(screen.getByRole("button", { name: "Сохранить черновик статьи" }));

    expect(await screen.findByRole("heading", { name: "Стратегическая сессия · v1" })).toBeInTheDocument();
    expect(api.saveArticle).toHaveBeenCalledWith(campaignId, expect.objectContaining({ illustration_version_id: "visual-1" }), undefined);
    fireEvent.click(screen.getByRole("button", { name: "Утвердить версию v1" }));
    await waitFor(() => expect(api.approve).toHaveBeenCalledWith(campaignId, "article-1"));
  });

  it("opens an exact historical media variant and starts edits from the latest version", async () => {
    storeVersion(illustration({ approval }));
    storeVersion(illustration({ id: "visual-2", version_number: 2, supersedes_version_id: "visual-1" }));
    render(<StudioWorkspace campaignId={campaignId} />);
    fireEvent.click(await library().findByRole("button", { name: "Иллюстрация для статьи" }));
    fireEvent.click(await screen.findByRole("button", { name: "Смотреть v1" }));
    expect(screen.getByRole("heading", { name: "Иллюстрация для статьи · v1" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Вариант предпросмотра"), { target: { value: "square" } });
    await waitFor(() => expect(api.media).toHaveBeenCalledWith(campaignId, "visual-1", "square", expect.any(AbortSignal)));
    fireEvent.click(screen.getByRole("button", { name: "Новая версия на основе v2" }));
    expect(screen.getByText("Новая версия иллюстрации на основе v2")).toBeInTheDocument();
    expect(screen.getByLabelText("Бриф иллюстрации")).toHaveValue("Команда у доски");
    expect(screen.getByLabelText("Как получить изображение")).toHaveValue("keep");
    expect(screen.queryByLabelText("Файл изображения")).not.toBeInTheDocument();
    api.uploadIllustration.mockImplementationOnce(async () => storeVersion(illustration({
      id: "visual-3", version_number: 3, supersedes_version_id: "visual-2",
    })));
    fireEvent.click(screen.getByRole("button", { name: "Сохранить новую версию" }));
    expect(await screen.findByRole("heading", { name: "Иллюстрация для статьи · v3" })).toBeInTheDocument();
    expect(api.uploadIllustration).toHaveBeenCalledWith(campaignId, expect.objectContaining({ usage_rights: "Собственное изображение" }), undefined, {
      asset_id: "visual-asset", expected_current_version_id: "visual-2",
    });
  });

  it("keeps local edits and links while explicitly switching to the latest revision after a conflict", async () => {
    const linkedIllustration = storeVersion(illustration({ approval }));
    const initial = storeVersion(article({ source_version_id: "source-1", illustration_version_id: null }));
    render(<StudioWorkspace campaignId={campaignId} />);
    fireEvent.click(await library().findByRole("button", { name: initial.payload.title }));
    fireEvent.click(await screen.findByRole("button", { name: "Смотреть v1" }));
    fireEvent.click(screen.getByRole("button", { name: "Новая версия на основе v1" }));
    fireEvent.change(screen.getByLabelText("Название статьи"), { target: { value: "Локальный заголовок" } });
    fireEvent.change(screen.getByLabelText("Текст статьи"), { target: { value: "Локальные правки" } });
    fireEvent.click(screen.getByRole("button", { name: "Выбрать · версия иллюстрации" }));
    fireEvent.click(await screen.findByRole("button", { name: `${linkedIllustration.payload.title} · иллюстрация` }));
    fireEvent.click(await screen.findByRole("button", { name: "Выбрать v1 · утверждена" }));

    const latest = storeVersion(article({
      id: "article-2",
      version_number: 2,
      supersedes_version_id: initial.id,
      illustration_version_id: null,
      payload: { ...initial.payload, body: "Правки другого редактора" },
    }));
    api.assets.mockResolvedValue(page([{ id: initial.asset_id, kind: initial.kind, latest_version: initial }]));
    const conflict = new ApiError("Материал изменился. Откройте новую версию.", 409, "STUDIO_VERSION_CONFLICT");
    api.saveArticle.mockRejectedValueOnce(conflict);
    api.versions
      .mockRejectedValueOnce(new Error("Временная ошибка загрузки"))
      .mockRejectedValueOnce(new ApiError("Требуется авторизация.", 401, "AUTHENTICATION_REQUIRED"));
    fireEvent.click(screen.getByRole("button", { name: "Сохранить черновик статьи" }));

    const retryVersion = await screen.findByRole("button", { name: "Повторить загрузку актуальной версии" });
    expect(screen.getByRole("alert")).toHaveTextContent("Ваши правки остались в форме.");
    expect(screen.getByLabelText("Текст статьи")).toHaveValue("Локальные правки");
    expect(api.saveArticle).toHaveBeenCalledTimes(1);
    expect(api.saveArticle).toHaveBeenLastCalledWith(campaignId, expect.objectContaining({ body: "Локальные правки" }), {
      asset_id: initial.asset_id,
      expected_current_version_id: initial.id,
    });

    const save = screen.getByRole<HTMLButtonElement>("button", { name: "Сохранить черновик статьи" });
    expect(save).toBeDisabled();
    fireEvent.submit(save.form!);
    expect(api.saveArticle).toHaveBeenCalledTimes(1);

    fireEvent.click(retryVersion);
    const signIn = await screen.findByRole("link", { name: "Войти в новой вкладке" });
    expect(signIn).toHaveAttribute("href", "/login");
    expect(signIn).toHaveAttribute("target", "_blank");
    expect(screen.getByText(/Ваши правки остались в форме/)).toBeInTheDocument();
    expect(screen.getByLabelText("Текст статьи")).toHaveValue("Локальные правки");
    expect(save).toBeDisabled();
    fireEvent.submit(save.form!);
    expect(api.saveArticle).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Повторить загрузку актуальной версии" }));
    expect(await screen.findByText(/Доступна версия v2/)).toBeInTheDocument();
    expect(signIn).not.toBeInTheDocument();
    expect(screen.getByLabelText("Текст статьи")).toHaveValue("Локальные правки");
    expect(save).toBeDisabled();
    expect(api.saveArticle).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByText("Посмотреть актуальную версию v2"));
    expect(screen.getByText("Правки другого редактора")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Утвердить версию v2" })).not.toBeInTheDocument();
    expect(save).toBeDisabled();
    expect(await library().findByText(/Статья · v2/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Использовать v2 как основу" }));
    expect(screen.getByText("Новая редакция статьи на основе v2")).toBeInTheDocument();
    expect(screen.getByText("Посмотреть актуальную версию v2")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Использовать v2 как основу" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Название статьи")).toHaveValue("Локальный заголовок");
    expect(screen.getByLabelText("Текст статьи")).toHaveValue("Локальные правки");
    expect(screen.getByText(/source-1/)).toBeInTheDocument();
    expect(screen.getByText(/Иллюстрация для статьи · v1/)).toBeInTheDocument();
    expect(save).toBeEnabled();
    expect(api.saveArticle).toHaveBeenCalledTimes(1);

    api.saveArticle.mockImplementationOnce(async () => storeVersion(article({
      id: "article-3",
      version_number: 3,
      supersedes_version_id: latest.id,
      source_version_id: initial.source_version_id,
      illustration_version_id: linkedIllustration.id,
      payload: { title: "Локальный заголовок", body: "Локальные правки" },
    })));
    fireEvent.click(save);

    expect(await screen.findByRole("heading", { name: "Локальный заголовок · v3" })).toBeInTheDocument();
    expect(screen.queryByText("Посмотреть актуальную версию v2")).not.toBeInTheDocument();
    expect(api.saveArticle).toHaveBeenCalledTimes(2);
    expect(api.saveArticle).toHaveBeenLastCalledWith(campaignId, {
      title: "Локальный заголовок",
      body: "Локальные правки",
      source_version_id: initial.source_version_id,
      illustration_version_id: linkedIllustration.id,
    }, {
      asset_id: latest.asset_id,
      expected_current_version_id: latest.id,
    });
  });

  it("retains the chosen upload file when an illustration revision switches its base", async () => {
    const initial = storeVersion(illustration());
    render(<StudioWorkspace campaignId={campaignId} />);
    fireEvent.click(await library().findByRole("button", { name: initial.payload.title }));
    fireEvent.click(screen.getByRole("button", { name: "Новая версия на основе v1" }));
    fireEvent.change(screen.getByLabelText("Как получить изображение"), { target: { value: "upload" } });
    const file = new File(["new image"], "new-image.png", { type: "image/png" });
    const fileInput = screen.getByLabelText<HTMLInputElement>("Файл изображения");
    fireEvent.change(fileInput, { target: { files: [file] } });

    const latest = storeVersion(illustration({ id: "visual-2", version_number: 2, supersedes_version_id: initial.id }));
    api.uploadIllustration.mockRejectedValueOnce(new ApiError("Материал изменился.", 409, "STUDIO_VERSION_CONFLICT"));
    fireEvent.submit(fileInput.form!);
    fireEvent.click(await screen.findByRole("button", { name: "Использовать v2 как основу" }));

    expect(screen.getByText("Новая версия иллюстрации на основе v2")).toBeInTheDocument();
    expect(screen.getByLabelText("Файл изображения")).toBe(fileInput);
    expect(fileInput.files?.[0]).toBe(file);
    expect(api.uploadIllustration).toHaveBeenCalledTimes(1);

    api.uploadIllustration.mockImplementationOnce(async () => storeVersion(illustration({
      id: "visual-3", version_number: 3, supersedes_version_id: latest.id,
    })));
    fireEvent.submit(fileInput.form!);
    expect(await screen.findByRole("heading", { name: `${initial.payload.title} · v3` })).toBeInTheDocument();
    expect(api.uploadIllustration).toHaveBeenLastCalledWith(campaignId, expect.objectContaining({ title: initial.payload.title }), file, {
      asset_id: initial.asset_id, expected_current_version_id: latest.id,
    });
  });

  it("keeps the illustration form open when dispatch fails immediately", async () => {
    const brief = "Локальный бриф для новой иллюстрации";
    api.generate.mockResolvedValue({
      id: "job-1", status: "FAILED", asset_id: null, version_id: null,
      error_code: "CREATIVE_GENERATION_QUEUE_UNAVAILABLE",
    });
    render(<StudioWorkspace campaignId={campaignId} />);
    fireEvent.click(screen.getByRole("button", { name: "Новая иллюстрация" }));
    fillIllustration();
    fireEvent.change(screen.getByLabelText("Бриф иллюстрации"), { target: { value: brief } });
    fireEvent.change(screen.getByLabelText("Как получить изображение"), { target: { value: "generate" } });
    fireEvent.click(screen.getByRole("button", { name: "Начать генерацию" }));

    expect(await screen.findByText("Генерация завершилась ошибкой.")).toBeInTheDocument();
    expect(screen.getByText("CREATIVE_GENERATION_QUEUE_UNAVAILABLE")).toBeInTheDocument();
    expect(screen.getByLabelText("Бриф иллюстрации")).toHaveValue(brief);
    expect(screen.getByLabelText("Как получить изображение")).toHaveValue("generate");
    expect(api.generate).toHaveBeenCalledExactlyOnceWith(campaignId, expect.objectContaining({ brief }), undefined);
    expect(api.generationJob).not.toHaveBeenCalled();
  });

  it("restores submitted illustration edits after a queued generation hits a version conflict", async () => {
    const initial = illustration();
    storeVersion(initial);
    const brief = "Локальный бриф для новой версии";
    const caption = "Локальная подпись";
    api.generate.mockResolvedValue({
      id: "job-1", status: "PENDING", asset_id: initial.asset_id, version_id: null, error_code: null,
    });
    api.generationJob.mockImplementation(async () => {
      storeVersion(illustration({
        id: "visual-2", version_number: 2, supersedes_version_id: initial.id,
        payload: { ...initial.payload, brief: "Бриф другого редактора", caption: "Другая подпись" },
      }));
      return {
        id: "job-1", status: "FAILED", asset_id: initial.asset_id, version_id: null,
        error_code: "STUDIO_VERSION_CONFLICT",
      };
    });
    render(<StudioWorkspace campaignId={campaignId} />);
    fireEvent.click(await library().findByRole("button", { name: initial.payload.title }));
    fireEvent.click(screen.getByRole("button", { name: "Новая версия на основе v1" }));
    fireEvent.change(screen.getByLabelText("Бриф иллюстрации"), { target: { value: brief } });
    fireEvent.change(screen.getByLabelText("Подпись"), { target: { value: caption } });
    fireEvent.change(screen.getByLabelText("Как получить изображение"), { target: { value: "generate" } });
    fireEvent.click(screen.getByRole("button", { name: "Начать генерацию" }));

    const restore = await screen.findByRole("button", { name: "Вернуться к брифу" });
    expect(screen.getByText("Генерация завершилась ошибкой.")).toBeInTheDocument();
    expect(screen.queryByLabelText("Бриф иллюстрации")).not.toBeInTheDocument();
    fireEvent.click(restore);

    expect(await screen.findByText(/Доступна версия v2/)).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Ваши правки остались в форме.");
    expect(screen.getByLabelText("Бриф иллюстрации")).toHaveValue(brief);
    expect(screen.getByLabelText("Подпись")).toHaveValue(caption);
    expect(screen.getByLabelText("Как получить изображение")).toHaveValue("generate");
    expect(screen.getByRole("button", { name: "Начать генерацию" })).toBeDisabled();
    expect(api.generate).toHaveBeenCalledExactlyOnceWith(campaignId, expect.objectContaining({ brief, caption }), {
      asset_id: initial.asset_id, expected_current_version_id: initial.id,
    });
    expect(api.generationJob).toHaveBeenCalledWith(campaignId, "job-1", expect.any(AbortSignal));

    fireEvent.click(screen.getByRole("button", { name: "Отмена" }));
    fireEvent.click(await screen.findByRole("button", { name: "Новая версия на основе v2" }));
    expect(screen.getByLabelText("Бриф иллюстрации")).toHaveValue("Бриф другого редактора");
  });


  it.each([
    ["FAILED", "Генерация завершилась ошибкой."],
    ["UNKNOWN", "Результат генерации не подтверждён."],
  ] as const)("restores a submitted brief after a reload when generation is %s", async (status, message) => {
    const previous = storeVersion(illustration({ approval }));
    const brief = "Бриф, сохранённый с заданием";
    const storageKey = `iteam:creative-generation:${campaignId}`;
    sessionStorage.setItem(storageKey, "job-restored");
    api.generationJob.mockResolvedValue({
      id: "job-restored",
      status,
      asset_id: previous.asset_id,
      version_id: null,
      error_code: "CREATIVE_GENERATION_ERROR",
      recovery: {
        draft: {
          title: "Иллюстрация из задания",
          brief,
          brand_profile: "Правила бренда",
          alt_text: "Описание изображения",
          usage_rights: "Собственное изображение",
          caption: null,
          source_version_id: previous.id,
        },
        previous,
      },
    });
    render(<StudioWorkspace campaignId={campaignId} />);

    fireEvent.click(await screen.findByRole("button", { name: "Вернуться к брифу" }));
    expect(screen.getByText(message)).toBeInTheDocument();
    expect(screen.getByText("Новая версия иллюстрации на основе v1")).toBeInTheDocument();
    expect(screen.getByLabelText("Название иллюстрации")).toHaveValue("Иллюстрация из задания");
    expect(screen.getByLabelText("Бриф иллюстрации")).toHaveValue(brief);
    expect(screen.getByLabelText("Подпись")).toHaveValue("");
    expect(sessionStorage.getItem(storageKey)).toBe("job-restored");
    expect(api.generationJob).toHaveBeenCalledWith(campaignId, "job-restored", expect.any(AbortSignal));
    expect(api.generate).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Отмена" }));
    fireEvent.click(screen.getByRole("button", { name: "Скрыть задачу" }));
    expect(sessionStorage.getItem(storageKey)).toBeNull();
    expect(screen.queryByText(message)).not.toBeInTheDocument();
  });

  it("can check an existing generation again after signing back in", async () => {
    const storageKey = `iteam:creative-generation:${campaignId}`;
    sessionStorage.setItem(storageKey, "job-existing");
    api.generationJob
      .mockRejectedValueOnce(new ApiError("Требуется авторизация.", 401, "AUTHENTICATION_REQUIRED"))
      .mockResolvedValueOnce({
        id: "job-existing", status: "FAILED", asset_id: null, version_id: null,
        error_code: "CREATIVE_GENERATION_ERROR", recovery: null,
      });
    render(<StudioWorkspace campaignId={campaignId} />);

    const signIn = await screen.findByRole("link", { name: "Войти в новой вкладке" });
    expect(signIn).toHaveAttribute("href", "/login");
    expect(sessionStorage.getItem(storageKey)).toBe("job-existing");
    fireEvent.click(screen.getByRole("button", { name: "Проверить состояние" }));

    expect(await screen.findByText("Генерация завершилась ошибкой.")).toBeInTheDocument();
    expect(api.generationJob).toHaveBeenCalledTimes(2);
    expect(api.generate).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(storageKey)).toBeNull();
    expect(screen.queryByRole("button", { name: "Вернуться к брифу" })).not.toBeInTheDocument();
  });

  it("tracks generation by job, then opens the exact generated version and its provenance", async () => {
    const draft = illustration();
    const generated = illustration({ payload: { ...draft.payload, provenance: { source: "GENERATED", model: "studio-model", prompt: "Нарисуй команду" } } });
    api.generate.mockResolvedValue({ id: "job-1", status: "PENDING", asset_id: null, version_id: null, error_code: null });
    api.generationJob.mockImplementation(async () => {
      storeVersion(generated);
      for (let number = 2; number <= 51; number += 1) {
        storeVersion(illustration({ id: `visual-${number}`, version_number: number, supersedes_version_id: `visual-${number - 1}` }));
      }
      return { id: "job-1", status: "SUCCEEDED", asset_id: draft.asset_id, version_id: draft.id, error_code: null };
    });
    render(<StudioWorkspace campaignId={campaignId} />);
    fireEvent.click(screen.getByRole("button", { name: "Новая иллюстрация" }));
    fillIllustration();
    fireEvent.change(screen.getByLabelText("Как получить изображение"), { target: { value: "generate" } });
    fireEvent.click(screen.getByRole("button", { name: "Начать генерацию" }));
    fireEvent.click(await screen.findByRole("button", { name: "Открыть черновик иллюстрации" }));

    expect(await screen.findByRole("heading", { name: "Иллюстрация для статьи · v1" })).toBeInTheDocument();
    expect(screen.getByText("studio-model")).toBeInTheDocument();
    expect(screen.getByText("Нарисуй команду")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Новая версия на основе v51" })).toBeInTheDocument();
    expect(api.version).toHaveBeenCalledExactlyOnceWith(campaignId, generated.id, expect.any(AbortSignal));
    expect(api.versions).toHaveBeenCalledWith(campaignId, generated.asset_id, 0, expect.any(AbortSignal), 1);
    expect(api.generate).toHaveBeenCalledWith(campaignId, expect.objectContaining({ usage_rights: "Собственное изображение" }), undefined);
    expect(api.generationJob).toHaveBeenCalledWith(campaignId, "job-1", expect.any(AbortSignal));
    expect(api.uploadIllustration).not.toHaveBeenCalled();
  });
});
