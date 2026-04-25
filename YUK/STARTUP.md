# PIS-JKalachand Startup

Run these every time before working on the project.

---

## 1. Make sure Docker Desktop is open

Just launch the app — no command needed. The PostgreSQL container starts automatically if Docker Desktop is running.

Verify the DB container is up:
```cmd
docker ps
```
You should see `pis-jkalachand-main-db-1` with status `Up`.

If it's not running, start it:
```cmd
docker start pis-jkalachand-main-db-1
```

---

## 2. Run the app

**Command Prompt:**
```cmd
cd C:\Users\yukta\PIS-JKalachand
set PYTHONUTF8=1
venv\Scripts\python app.py
```

**PowerShell:**
```powershell
cd C:\Users\yukta\PIS-JKalachand
$env:PYTHONUTF8=1
.\venv\Scripts\python app.py
```

---

## 3. Open in browser

```
http://localhost:5000
```

Login: `admin@jkalachand.com` / `admin123`

---

## Stop the app

Press `Ctrl+C` in the terminal.
