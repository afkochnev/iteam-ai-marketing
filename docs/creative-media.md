# Медиа в студии кампании

## Локальная проверка

Запустите стенд из отдельной копии проекта с Docker Compose 2.24.4 или новее.
Подготовьте отдельный `.env` на основе `.env.example` и задайте тестовые данные
администратора. Оставьте `CREATIVE_IMAGE_MODEL` и `OPENAI_API_KEY` пустыми для
проверки загрузки. В этой конфигурации достаточно локального изображения PNG,
JPEG или WebP, на которое у вас есть права.

Команды ниже рассчитаны на новую базу стенда. Начальная миграция до
`20261121_0029` позволяет создать тестового администратора до того, как
миграция `20261121_0030` назначит ему членство в рабочем пространстве.

```powershell
$creativeCompose = @("-p", "iteam-creative-review", "-f", "docker-compose.yml", "-f", "docker-compose.creative.yml")
docker compose @creativeCompose up -d postgres redis
docker compose @creativeCompose run --rm --build backend alembic upgrade 20261121_0029
docker compose @creativeCompose run --rm backend python -m app.seed
docker compose @creativeCompose run --rm backend alembic upgrade head
docker compose @creativeCompose up -d --build backend frontend
```

Войдите на [стенд студии](http://localhost:13000/campaigns), создайте кампанию
и откройте студию. Создайте черновик статьи, задайте бриф и загрузите иллюстрацию
с alt-текстом и сведениями о происхождении. Проверьте предпросмотр оригинала
и вариантов для сайта, VK, email и презентации. Утвердите версию иллюстрации,
свяжите её точную версию с новой редакцией статьи и утвердите эту редакцию.
Проверку вызова генератора выполняет тест с подменённым ответом OpenAI
`backend/app/tests/test_creative_media.py`.

Конфигурация генерации принимает отдельный идентификатор модели в
`CREATIVE_IMAGE_MODEL`, ключ `OPENAI_API_KEY` и общий срок вызова
`CREATIVE_IMAGE_TIMEOUT_SECONDS`. За задания отвечает `ai_worker`. Для
восстановления статусов зависших заданий используются `ai_scheduler` и
`ai_control_worker`.

## Тестовая CMS

Для отдельного стенда с `APP_ENV=development` добавьте в его `.env`
`STUDIO_WEBSITE_SANDBOX_ENABLED=true` и примените миграцию `20261125_0037`.
Для параллельной проверки в отдельном окне PowerShell задайте параметры:

```powershell
$env:CREATIVE_REVIEW_FRONTEND_PORT = "13001"
$env:CREATIVE_REVIEW_BACKEND_PORT = "18001"
$env:CREATIVE_REVIEW_POSTGRES_PORT = "15434"
$env:CREATIVE_REVIEW_AUTH_COOKIE_NAME = "iteam_website_review"
```

В командах запуска выше используйте другое имя проекта Compose, например
`iteam-website-review`. Это создаст отдельные тома. После входа на
[тестовый стенд](http://localhost:13001/campaigns) раздел появится в студии
у утверждённой версии статьи.

1. Укажите SEO-заголовок и описание, если они нужны. Создайте черновик.
2. Откройте предпросмотр и проверьте текст и иллюстрацию. Для просмотра
   черновика нужен вход в iTeam.
3. Нажмите «Опубликовать на тестовом сайте». Откройте ссылку на публикацию
   в браузере без входа. Повтор команды возвращает тот же ID страницы.

Тестовая CMS хранит черновик и опубликованную страницу в базе стенда.
Для этого сценария используются только тестовые материалы.

## Тестовый WordPress

В отдельном экземпляре iTeam с APP_ENV=development или APP_ENV=test укажите в
файле .env адрес тестового WordPress в STUDIO_WORDPRESS_URL и числовой ID его
учётной записи в STUDIO_WORDPRESS_AUTHOR_ID. Учётной записи нужны права
создавать и публиковать свои записи и загружать медиа. Для удалённого стенда
используется HTTPS.

Сохраните username и application_password в JSON-файле вне checkout.
Исполнитель получает файл как секрет Compose, доступный пользователю
контейнера (UID 10001). Укажите путь к файлу в
STUDIO_WORDPRESS_CREDENTIALS_HOST_FILE перед запуском.

Команды рассчитаны на новую базу стенда. Создайте тестового администратора до
миграции, которая добавит его в рабочее пространство.

```powershell
$env:CREATIVE_REVIEW_FRONTEND_PORT = "13002"
$env:CREATIVE_REVIEW_BACKEND_PORT = "18002"
$env:CREATIVE_REVIEW_POSTGRES_PORT = "15435"
$env:CREATIVE_REVIEW_AUTH_COOKIE_NAME = "iteam_wordpress_review"
$wpCompose = @('compose', '-p', 'iteam-wordpress-review', '-f', 'docker-compose.yml',
    '-f', 'docker-compose.creative.yml', '-f', 'docker-compose.studio-wordpress.yml')
docker @wpCompose up -d --wait postgres redis
docker @wpCompose build backend frontend studio_wordpress_worker
docker @wpCompose run --rm --no-deps backend alembic upgrade 20261121_0029
docker @wpCompose run --rm --no-deps backend python -m app.seed
docker @wpCompose run --rm --no-deps backend alembic upgrade head
docker @wpCompose up -d backend frontend studio_wordpress_worker
```

Панель WordPress появляется у утверждённой версии статьи в Studio. Создание
черновика и публикация требуют отдельных действий. Подтверждённый черновик можно
открыть в WordPress перед публикацией. Для предпросмотра нужен вход в WordPress
с правом просмотра черновика. При публикации исполнитель
загружает утверждённую иллюстрацию как главное изображение записи. Для каждой
команды он сохраняет попытку в PostgreSQL до вызова WordPress. После выполнения
Studio показывает ID записи и публичную ссылку. В случае неизвестного результата
кнопка сверки проверяет исход по данным WordPress. Если запись или иллюстрацию
не удалось подтвердить, операция остаётся в статусе неопределённости.
Если сама сверка завершилась ошибкой, Studio показывает её отдельно. Код ошибки
сверки доступен в технических сведениях рядом с результатом сверки.
Если Studio потеряла ответ API iTeam на команду, нажмите «Обновить статус
WordPress». Studio прочитает сохранённое состояние и покажет принятую команду
или её результат без повторной отправки.

## Хранение

Том `creative_media` доступен бэкенду и исполнителю генерации. Оригиналы и
варианты лежат в подкаталогах по SHA-256 содержимого. PostgreSQL хранит ссылки
на объекты, размеры и MIME-типы, сведения о происхождении и сохранённые
параметры генерации. Бэкенд выдаёт изображение после проверки доступа к версии
материала.

Вместе со снимком PostgreSQL сохраните копию приватного тома, остановив запись
в бэкенде и исполнителе генерации на время резервного копирования. Восстанавливайте
их как пару. Сохраните структуру ключей и права доступа к каталогу для UID 10001.
В восстановленной копии проверьте отображение оригинала и вариантов и совпадение
их SHA-256 с метаданными версий.
