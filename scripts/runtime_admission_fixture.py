"""Synthetic candidate-only probes, executed inside the newly built backend.

No application module is imported by the host. Payloads emit hashes, fixed
diagnostics and synthetic identifiers; neither table rows nor credentials leave
the disposable runtime.
"""

DATABASE = "iteam_runtime_admission_drill"
CHECKOUT_SCHEMA = "20261123_0033"
USER_ID = "00000000-0000-4000-8000-000000000001"
RUN_ID = "00000000-0000-4000-8000-000000000005"
CHAT_ID = "00000000-0000-4000-8000-000000000010"
CANARY_NAME = "Runtime admission committed canary"

SEED_CODE = r'''
import asyncio,hashlib,json,sys
from datetime import UTC,datetime,timedelta
from uuid import UUID
from sqlalchemy import text
from app.core.database import async_session_factory,engine
from app.core.security import hash_password
from app.models import *
from app.models.marketing_chat import MarketingMessageRole,MarketingMessageStatus
def uid(n): return UUID('00000000-0000-4000-8000-'+str(n).zfill(12))
async def main():
 async with async_session_factory() as s:
  assert await s.scalar(text('SELECT current_database()'))=='iteam_runtime_admission_drill'
  assert (await s.scalars(text('SELECT version_num FROM alembic_version'))).all()==[sys.argv[1]]
  old=datetime.now(UTC)-timedelta(days=2)
  s.add(User(id=uid(1),email='candidate@example.invalid',password_hash=hash_password('fixture-only-password'),full_name='Candidate fixture',role=UserRole.ADMIN,is_active=True,auth_version=1))
  s.add(Agent(id=uid(2),name='Candidate writer',slug='writer',role='content_writer',system_prompt='Synthetic admission fixture.',model='gpt-4.1-mini',status=AgentStatus.ACTIVE,autonomy_level=2,settings={}))
  await s.flush()
  s.add(Campaign(id=uid(3),name='Admission fixture',goal='Test passive runtime',status=CampaignStatus.ACTIVE,created_by=uid(1),strategy={},strategy_version=1))
  await s.flush()
  for number,status in ((4,TaskStatus.READY),(6,TaskStatus.IN_PROGRESS),(8,TaskStatus.READY),(12,TaskStatus.READY)):
   s.add(Task(id=uid(number),campaign_id=uid(3),task_type=TaskType.MANUAL,title='Admission task '+str(number),assigned_agent_id=uid(2),priority=TaskPriority.NORMAL,status=status,input_data={},output_data={},started_at=old if number==6 else None))
  await s.flush()
  for number,task,status in ((5,4,AgentRunStatus.QUEUED),(7,6,AgentRunStatus.RUNNING),(9,8,AgentRunStatus.QUEUED)):
   s.add(AgentRun(id=uid(number),agent_id=uid(2),task_id=uid(task),campaign_id=uid(3),status=status,input_data={'text':'Synthetic admission canary','allowed_knowledge_pack_ids':[],'allowed_content_version_ids':[]},model='gpt-4.1-mini',prompt_snapshot='Synthetic fixture.',prompt_hash=hashlib.sha256(b'Synthetic fixture.').hexdigest(),started_at=old if number==7 else None,created_at=old,queue_job_id=None))
  s.add(MarketingConversation(id=uid(10),campaign_id=uid(3),created_by_user_id=uid(1),title='Stale conversation'))
  await s.flush()
  s.add(MarketingMessage(id=uid(11),conversation_id=uid(10),role=MarketingMessageRole.ASSISTANT,status=MarketingMessageStatus.PENDING,content='',task_id=uid(8),agent_run_id=uid(9)))
  s.add(ContentItem(id=uid(13),campaign_id=uid(3),source_task_id=uid(12),content_type=ContentType.SOCIAL_POST,title='Never publish',status=ContentStatus.APPROVED,author_agent_id=uid(2),channel=ContentChannel.TELEGRAM,metadata_={}))
  await s.flush()
  s.add(ContentVersion(id=uid(14),content_item_id=uid(13),version_number=1,content='Synthetic admission canary. Never publish.',structured_content={},created_by_user_id=uid(1)))
  await s.flush()
  await s.execute(text('UPDATE content_items SET current_version_id=:v WHERE id=:i'),{'v':uid(14),'i':uid(13)})
  for number,channel in ((15,ContentChannel.TELEGRAM),(16,ContentChannel.VK)):
   s.add(Publication(id=uid(number),campaign_id=uid(3),content_item_id=uid(13),content_version_id=uid(14),channel=channel,status=PublicationStatus.PUBLISHING,approved_for_publish_at=old,approved_for_publish_by=uid(1),execution_token=str(uid(number)),retry_count=0))
  default=await s.scalar(text('SELECT id FROM workspaces WHERE is_default'))
  assert default is not None
  s.add(WorkspaceMembership(id=uid(17),workspace_id=default,user_id=uid(1),role=WorkspaceRole.WORKSPACE_ADMIN,is_active=True))
  await s.commit()
 await engine.dispose()
asyncio.run(main())
from app.workers.celery_app import celery_app
messages=[('execute_agent_run',[str(uid(5))],'ai'),('recover_stuck_ai_tasks',[],'ai_control'),('dispatch_ready_tasks',[],'ai_control'),('publish_telegram_publication',[str(uid(15)),str(uid(15))],'publication'),('publish_vk_publication',[str(uid(16)),str(uid(16))],'publication'),('recover_stuck_publications',[],'publication_control'),('sync_recent_publication_metrics',[],'metrics')]
for index,(task,args,queue) in enumerate(messages,101):
 celery_app.send_task(task,args=args,queue=queue,task_id=str(uid(index)))
print(json.dumps({'status':'PASS','queued_canaries':len(messages),'stale_get_canary':True,'stale_recovery_canary':True,'claimed_publication_canaries':2}))
'''

SNAPSHOT_CODE = r'''
import asyncio,hashlib,json,os,re
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
def hashed(value): return 'sha256:'+hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
async def main():
 engine=create_async_engine(os.environ['DATABASE_URL'])
 async with engine.connect() as c:
  c=await c.execution_options(isolation_level='REPEATABLE READ')
  async with c.begin():
   await c.execute(text('SET TRANSACTION READ ONLY'))
   assert await c.scalar(text('SELECT current_database()'))=='iteam_runtime_admission_drill'
   names=(await c.execute(text("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='public' ORDER BY tablename"))).scalars().all()
   assert names and all(re.fullmatch(r'[a-z_][a-z0-9_]{0,62}',n) for n in names)
   rows={}
   for name in names: rows[name]=sorted((await c.execute(text('SELECT to_jsonb(t)::text FROM public."'+name+'" AS t'))).scalars().all())
   identity=(await c.execute(text("SELECT (SELECT system_identifier::text FROM pg_control_system()),(SELECT oid::bigint FROM pg_database WHERE datname=current_database())"))).one()
   claim=(await c.execute(text("SELECT status::text,started_at IS NOT NULL,completed_at IS NOT NULL FROM agent_runs WHERE id='00000000-0000-4000-8000-000000000005'"))).one()
   canaries=await c.scalar(text("SELECT count(*) FROM campaigns WHERE name='Runtime admission committed canary'"))
   stale_get=await c.scalar(text("SELECT status::text FROM agent_runs WHERE id='00000000-0000-4000-8000-000000000009'"))
   schema=(await c.scalars(text('SELECT version_num FROM alembic_version'))).one()
   print(json.dumps({'schema':schema,'database_identity_sha256':hashed(list(identity)),'all_tables_sha256':hashed(rows),'table_counts':{k:len(v) for k,v in rows.items()},'claim_status':claim[0],'claim_started':claim[1],'claim_finished':claim[2],'committed_canary_count':canaries,'stale_get_status':stale_get},sort_keys=True))
 await engine.dispose()
asyncio.run(main())
'''

REDIS_SNAPSHOT_CODE = r'''
import hashlib,json,os
import redis
r=redis.Redis.from_url(os.environ['REDIS_URL'])
values={key.hex():r.dump(key).hex() for key in sorted(r.scan_iter())}
assert values
print(json.dumps({'sha256':'sha256:'+hashlib.sha256(json.dumps(values,sort_keys=True,separators=(',',':')).encode()).hexdigest(),'key_count':len(values)}))
r.close()
'''

HTTP_CODE = r'''
import json,sys,urllib.request,urllib.error
from uuid import UUID
from app.core.config import settings
from app.core.security import encode_access_token
request=json.loads(sys.argv[1])
headers={'Cookie':settings.auth_cookie_name+'='+encode_access_token(UUID('00000000-0000-4000-8000-000000000001'),1),'Content-Type':'application/json'}
payload=None if 'body' not in request else json.dumps(request['body']).encode()
req=urllib.request.Request('http://backend:8000'+request['path'],data=payload,headers=headers,method=request.get('method','GET'))
try:
 with urllib.request.urlopen(req,timeout=8) as response: code=response.status;body=json.load(response)
except urllib.error.HTTPError as response: code=response.code;body=json.load(response)
print(json.dumps({'http_status':code,'body':body},sort_keys=True))
'''

PASSIVE_PROBES_CODE = r'''
import asyncio,json
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from app.core.runtime_admission import RuntimeAdmissionError
from app.core.database import async_session_factory,engine
from app.integrations.telegram import TelegramProvider,VKProvider
from app.integrations.openai_knowledge import OpenAIKnowledgeProvider
blocked=[]
async def provider(name,cls):
 try:
  value=cls()
  await value.publish(text='Synthetic admission probe',chat_id='fixture-only')
 except RuntimeAdmissionError as error:
  assert str(error)=='RUNTIME_PASSIVE'
  blocked.append(name)
 else: raise AssertionError('PROVIDER_GATE_NOT_ENFORCED')
async def main():
 await provider('telegram',TelegramProvider)
 await provider('vk',VKProvider)
 try: OpenAIKnowledgeProvider()
 except RuntimeAdmissionError as error:
  assert str(error)=='RUNTIME_PASSIVE'
  blocked.append('openai')
 else: raise AssertionError('PROVIDER_GATE_NOT_ENFORCED')
 try: async_session_factory()
 except RuntimeAdmissionError as error:
  assert str(error)=='RUNTIME_PASSIVE'
  blocked.append('database_session')
 else: raise AssertionError('DATABASE_GATE_NOT_ENFORCED')
 try:
  async with engine.begin() as c:
   assert await c.scalar(text('SHOW transaction_read_only'))=='on'
   await c.execute(text("UPDATE campaigns SET goal='Forbidden passive write' WHERE id='00000000-0000-4000-8000-000000000003'"))
 except DBAPIError as error: assert getattr(error.orig,'sqlstate',None)=='25006'
 else: raise AssertionError('DATABASE_NOT_READ_ONLY')
 await engine.dispose()
asyncio.run(main())
try:
 from app.workers.celery_app import celery_app
 celery_app.send_task('dispatch_ready_tasks',queue='ai_control')
except RuntimeAdmissionError as error:
 assert str(error)=='RUNTIME_PASSIVE'
 blocked.append('producer')
else: raise AssertionError('PRODUCER_GATE_NOT_ENFORCED')
print(json.dumps({'blocked':sorted(blocked),'direct_engine_read_only':True}))
'''

SINK_CODE = r'''
import json,socketserver,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
count=0
lock=threading.Lock()
class Sink(socketserver.BaseRequestHandler):
 def handle(self):
  global count
  with lock: count+=1
class Server(socketserver.ThreadingTCPServer):
 allow_reuse_address=True
 daemon_threads=True
for port in (80,443):
 server=Server(('0.0.0.0',port),Sink)
 threading.Thread(target=server.serve_forever,daemon=True).start()
class Status(BaseHTTPRequestHandler):
 def do_GET(self):
  with lock: data=json.dumps({'connections':count}).encode()
  self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
 def log_message(self,*args): pass
ThreadingHTTPServer(('0.0.0.0',8080),Status).serve_forever()
'''

SINK_PROBE_CODE = r'''
import json,socket,sys,time,urllib.request
if sys.argv[1]=='selftest':
 for host in ('api.openai.com','api.telegram.org','api.vk.com','api.vk.ru'):
  with socket.create_connection((host,443),timeout=3): pass
 time.sleep(.1)
with urllib.request.urlopen('http://provider-sink:8080',timeout=3) as response: print(response.read().decode())
'''
