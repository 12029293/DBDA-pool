@echo off
rem ASCII-only launcher for Task Scheduler (dola-pool uvicorn server)
rem clear PYTHONPATH: WorkBuddy 工具内启动会继承插件 shim，其 sitecustomize 改掉
rem shutil.rmtree（批量删账号会 SystemExit 打死 uvicorn）。计划任务本无此变量，保险起见清掉。
set "PYTHONPATH="
cd /d C:\Users\Administrator\Desktop\DBDA-pool\DBDA-pool-main
"C:\Users\Administrator\Desktop\DBDA-pool\DBDA-pool-main\.venv\Scripts\python.exe" -m uvicorn server:app --host 127.0.0.1 --port 8019 >> server_run.log 2>&1
