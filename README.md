# Сервис учёта заявок (aleksandr_kim)

Принимает заявки от пользователей в службу поддержки, ведёт их по статусам (новая, в работе, закрыта), хранит переписку комментариями и считает срок реакции по нормативу категории. Учебный сервис курсовой работы по дисциплине «Оптимизация клиент-серверных приложений», вариант 2.

## Требования

- Python 3.14 (работал на 3.14.4);
- PostgreSQL 16 (в контейнере, образ `postgres:16`);
- Docker с Docker Compose 2.x.

## Установка и запуск

    git clone <адрес репозитория> && cd aleksandr_kim-cw
    cp .env.example .env
    python3 -m venv .venv && . .venv/bin/activate
    pip install -r requirements.txt
    set -a && . ./.env && set +a
    docker compose up -d db
    psql_url_small=${DATABASE_URL%/*}/aleksandr_kim_small
    python scripts/seed.py small "$psql_url_small"
    docker compose exec db createdb -U app aleksandr_kim_work
    python scripts/seed.py work "$DATABASE_URL"
    uvicorn app.main:app --port $APP_PORT

Схема создаётся самим `scripts/seed.py` (файл `scripts/schema.sql`). Малое наполнение: 50 пользователей, 5 категорий, 300 заявок, 900 комментариев. Рабочее: 3000 пользователей, 10 категорий, 75 000 заявок, 225 000 комментариев. Генератор с фиксированным зерном, повторный запуск даёт те же данные. Для работы на малом объёме запустите сервис с `DATABASE_URL`, указывающим на `aleksandr_kim_small`.

## Переменные окружения

| Переменная   | Назначение                                | Пример                                               |
|--------------|-------------------------------------------|------------------------------------------------------|
| DB_PORT      | порт PostgreSQL на хосте                  | 5439                                                 |
| DATABASE_URL | подключение к базе данных                 | postgresql://app:changeme@localhost:5439/aleksandr_kim_work |
| ADMIN_URL    | административное подключение для тестов   | postgresql://app:changeme@localhost:5439/postgres    |
| APP_PORT     | порт сервиса                              | 8029                                                 |
| SECRET_KEY   | ключ подписи токенов                      | changeme                                             |

## Проверка работоспособности

Интерфейс открывается по адресу http://localhost:8029 . Учётные записи: `staff1` (сотрудник) и `user10` (пользователь), пароль у обеих `demo`. После входа виден список заявок, карточка и сводка.

## Тесты

    pytest -q

Бизнес-правила проверяются без базы, операции интерфейса — на отдельной базе `aleksandr_kim_test`, которая создаётся и наполняется малым объёмом автоматически.

## Замеры

    python scripts/bench.py http://localhost:8029 work 30

Прогрев 5 запросов, затем 30 повторов под учётной записью `staff1`. Время в базе и число запросов сервис возвращает в заголовках `X-DB-Time-Ms` и `X-DB-Queries`.

## Программный интерфейс

Все операции, кроме входа, требуют заголовок `Authorization: Bearer <токен>`.

| Метод и путь                      | Параметры                                  | Ответ                                                                    | Ошибки        |
|-----------------------------------|--------------------------------------------|--------------------------------------------------------------------------|---------------|
| POST /api/login                   | login, password                            | {"token", "role"}                                                        | 401, 422      |
| GET /api/categories               | —                                          | список {id, name, reaction_norm_minutes}                                 | 401           |
| GET /api/tickets                  | page, size (до 100), status, category_id   | {"items": [...], "total": N, "page", "size"}                             | 401, 422      |
| GET /api/tickets/{id}             | —                                          | заявка, comments, reaction_minutes, norm_violated                        | 401, 404      |
| POST /api/tickets                 | title, body, category_id                   | 201 и созданная заявка                                                   | 401, 404, 422 |
| POST /api/tickets/{id}/comments   | body                                       | 201 и созданный комментарий                                              | 401, 404, 409, 422 |
| POST /api/tickets/{id}/status     | status (in_progress, closed; только staff) | {id, status, closed_at}                                                  | 401, 403, 404, 409, 422 |
| GET /api/summary                  | —                                          | {open_by_category, open_total, violated, total, violated_share}          | 401           |
