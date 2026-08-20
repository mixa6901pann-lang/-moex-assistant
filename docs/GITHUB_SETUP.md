# Подключение MOEX Assistant к GitHub — пошаговая инструкция

Сервер готов, ключ сгенерирован, осталось тебе добавить remote и запушить.

## 1. Создай репозиторий на GitHub

1. Зайди на https://github.com/new
2. Repository name: `moex-assistant` (или как хочешь)
3. **Private** (код с торговым ботом не должен быть публичным)
4. **НЕ** ставь галки «Initialize with README / .gitignore / license» — у нас уже есть свой код и .gitignore
5. Create repository

## 2. Добавь deploy key

1. В репозитории: Settings → Deploy keys → Add deploy key
2. Title: `moex-assistant-server` (чтобы помнить, что это за ключ)
3. Key: вставь **публичный** ключ:
   ```
   ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPgeHyZ1xJbhQsTXDZavqm0Qv2mPE31n95oQJNbCnLsO moex-assistant@201.51.1.244
   ```
   (он также лежит на сервере: `/home/claude/.ssh/github_deploy_key.pub`)
4. **Поставь галку "Allow write access"** — иначе push не пройдёт
5. Add key

## 3. Добавь remote на сервере и запушь

Подключись к серверу и выполни:

```bash
# Подключение
ssh -p 2222 claude@201.51.1.244

# На сервере:
cd /root/moex
# Замени USER на свой GitHub username:
sudo git remote add origin git@github.com:USER/moex-assistant.git
sudo git push -u origin main
```

Если попросит пароль — что-то не так с ключом. Проверь:
```bash
sudo -u claude ssh -T git@github.com
```
Должно ответить `Hi USER! You've successfully authenticated...`

## 4. После успешного push

Pre-push hook будет блокировать прямой push в main. Чтобы пушить изменения:

```bash
cd /root/moex
sudo git checkout -b feature/my-change
# ... правки ...
sudo git add -A
sudo git commit -m "fix: ..."   # pre-commit проверит тесты
sudo git push -u origin feature/my-change
```

На GitHub: открой PR из `feature/my-change` в `main`, посмотри diff, нажми Merge. После merge локально:

```bash
sudo git checkout main
sudo git pull
```

## 5. Если сменишь GitHub-username или репозиторий

Просто пересоздай remote:
```bash
cd /root/moex
sudo git remote set-url origin git@github.com:NEW_USER/NEW_REPO.git
```

## Безопасность

- Deploy key привязан к **этому серверу** и **этому репо**. Если сменишь сервер — сгенерируй новый ключ и обнови на GitHub.
- Приватный ключ лежит в `/home/claude/.ssh/github_deploy_key` — НЕ коммить его (он уже в .gitignore через `id_*`).
- Если ключ скомпрометирован — удали его на GitHub и пересоздай.

## Восстановление с bundle (если GitHub недоступен)

```bash
git clone /root/backups/code/code-<timestamp>.bundle restored-code/
```

Бандлы лежат в `/root/backups/code/` за последние 30 дней.
