# VKino — пилот инструментов разработки (27.11.2026)

Ветка эксперимента. Не влияет на runtime бота, не изменяет настройки моделей и не требует рестарта. Существующий YouTube skill сохранён.

## Навыки
Ponytail, Addy Osmani: debugging-and-error-recovery, test-driven-development, code-review-and-quality. Копии оригинальных SKILL.md для Codex (.agents/skills) и Claude Code (.claude/skills), лицензии MIT. Это выбранные навыки, не полные плагины и не все 26 навыков. Дополнительные reference-файлы не включены (исходный путь вернул 404); для расширенных чеклистов обращаться к upstream https://github.com/addyosmani/agent-skills. При ошибках сначала воспроизвести, затем исправить и проверить; не ослаблять защиту, не удалять согласование сценария/сцен пользователем. Проверять diff и регрессионные тесты. Агенты должны подтвердить обнаружение навыков; существующие правила проекта имеют приоритет.

## Graphify
Официальный проект https://github.com/Graphify-Labs/graphify. В отдельном окружении разработчика: `uv tool install graphifyy` после проверки версии и зависимостей; затем `graphify install --project --platform codex` либо `graphify install --project` для Claude Code. Сначала индексировать только код локально в чистом checkout; исключить .env, секреты, базы, логи, медиа и резервные копии. Не отправлять данные на внешние сервисы. Не устанавливать глобально в production и не коммитить граф до проверки состава. Graphify пока не установлен.

## OmniRoute
Официальный проект https://github.com/cheaper-inference/OmniRoute. VPS: 1.9 GiB RAM, 653 MiB available, 8.3 GiB disk free, swap 511 MiB. Сервис пока не запускаем: риск OOM production. Тестировать отдельно, без публичного порта и без production-ключей, с проверенной закреплённой версией. Не заменять текущий резервный маршрут в shorts/ai.py до сравнения. Проверить 429, 503, timeout, обрезанный JSON, изображение, лимиты, корректный возврат ошибок и стоимость. Не расходовать платные кредиты без согласования. После успешного теста — отдельный feature flag с быстрым откатом. Бесплатный доступ не гарантирует безлимит.

## Источники (точные blob SHA)
- DietrichGebert/ponytail/LICENSE (blob 715d483338cea4365f0d91a27799cf61226d6bcf)
- addyosmani/agent-skills/LICENSE (blob d67778ada6b9cda6227e9130da182c13e73c8b2e)
- DietrichGebert/ponytail/skills/ponytail/SKILL.md (blob d64edd32265ba21cbf5270158f04e97d96925f27)
- addyosmani/agent-skills/skills/debugging-and-error-recovery/SKILL.md (blob a078674507959e787e0bc031aaf65fa919382375)
- addyosmani/agent-skills/skills/test-driven-development/SKILL.md (blob aa28050a4533a06fdd02e50102649705878e0b24)
- addyosmani/agent-skills/skills/code-review-and-quality/SKILL.md (blob 1c5ab78da82fc6965c0b57b295560327b19c6098)
