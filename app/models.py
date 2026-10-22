import os, time, uuid
from sqlalchemy import create_engine, String, Float, Integer, Boolean, JSON, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, mapped_column, sessionmaker

def uid(): return uuid.uuid4().hex
class Base(DeclarativeBase): pass

class User(Base):
    __tablename__='users'
    id=mapped_column(String,primary_key=True)
    username=mapped_column(String,unique=True)
    role=mapped_column(String,default='developer')
    enabled=mapped_column(Boolean,default=True)
    created=mapped_column(Float,default=time.time)

class Project(Base):
    __tablename__='projects'
    id=mapped_column(String,primary_key=True,default=uid)
    name=mapped_column(String,unique=True)
    quota=mapped_column(JSON,default=dict)
    approval=mapped_column(Boolean,default=False)
    created=mapped_column(Float,default=time.time)

class Member(Base):
    __tablename__='members'
    __table_args__=(UniqueConstraint('project_id','user_id'),)
    id=mapped_column(String,primary_key=True,default=uid)
    project_id=mapped_column(String,index=True)
    user_id=mapped_column(String,index=True)
    role=mapped_column(String,default='developer')
    quota=mapped_column(JSON,default=dict)

class Node(Base):
    __tablename__="nodes"
    id=mapped_column(String,primary_key=True)
    name=mapped_column(String)
    ip=mapped_column(String,unique=True)
    engine_id=mapped_column(String,unique=True)
    storage_id=mapped_column(String)
    enabled=mapped_column(Boolean,default=True)
    project_ids=mapped_column(JSON,default=list)
    created=mapped_column(Float,default=time.time)

class Image(Base):
    __tablename__='images'
    id=mapped_column(String,primary_key=True,default=uid)
    name=mapped_column(String)
    reference=mapped_column(String)
    project_id=mapped_column(String,nullable=True)
    enabled=mapped_column(Boolean,default=True)
    status=mapped_column(String,default='pending')
    digest=mapped_column(String,default='')
    error=mapped_column(String,default='')
    build_config=mapped_column(JSON,default=dict)
    created=mapped_column(Float,default=time.time)

class Flavor(Base):
    __tablename__='flavors'
    id=mapped_column(String,primary_key=True,default=uid)
    name=mapped_column(String)
    project_id=mapped_column(String,nullable=True)
    cpu=mapped_column(Float)
    memory_gib=mapped_column(Integer)
    gpu=mapped_column(Integer,default=0)
    enabled=mapped_column(Boolean,default=True)

class Volume(Base):
    __tablename__='volumes'
    id=mapped_column(String,primary_key=True,default=uid)
    name=mapped_column(String)
    project_id=mapped_column(String,index=True)
    owner_id=mapped_column(String,index=True)
    size_gib=mapped_column(Integer)
    elastic=mapped_column(Boolean,default=False)
    kind=mapped_column(String,default='generic')
    binding=mapped_column(JSON,default=dict)
    access=mapped_column(String,default='private')
    status=mapped_column(String,default='creating')
    quota_enforced=mapped_column(Boolean,default=False)
    error=mapped_column(String,default='')
    created=mapped_column(Float,default=time.time)

class Workspace(Base):
    __tablename__='workspaces'
    __table_args__=(UniqueConstraint('owner_id','request_key'),)
    id=mapped_column(String,primary_key=True,default=uid)
    name=mapped_column(String)
    project_id=mapped_column(String,index=True)
    owner_id=mapped_column(String,index=True)
    image_id=mapped_column(String)
    image_ref=mapped_column(String)
    node_id=mapped_column(String,index=True,default='primary')
    cpu=mapped_column(Float)
    memory_gib=mapped_column(Integer)
    gpu=mapped_column(Integer,default=0)
    gpu_mode=mapped_column(String,default='exclusive')
    mounts=mapped_column(JSON,default=list)
    status=mapped_column(String,default='queued',index=True)
    desired=mapped_column(String,default='running')
    devices=mapped_column(JSON,default=list)
    runtime_id=mapped_column(String,default='')
    access_info=mapped_column(JSON,default=dict)
    error=mapped_column(String,default='')
    public_key=mapped_column(String)
    expires=mapped_column(Float)
    created=mapped_column(Float,default=time.time)
    updated=mapped_column(Float,default=time.time)
    request_key=mapped_column(String)

class Audit(Base):
    __tablename__='audit'
    id=mapped_column(String,primary_key=True,default=uid)
    actor_id=mapped_column(String)
    actor=mapped_column(String)
    action=mapped_column(String)
    object_id=mapped_column(String,default='')
    project_id=mapped_column(String,nullable=True,index=True)
    detail=mapped_column(JSON,default=dict)
    created=mapped_column(Float,default=time.time)

class LoginSession(Base):
    __tablename__='login_sessions'
    id=mapped_column(String,primary_key=True)
    user_id=mapped_column(String)
    expires=mapped_column(Float)

class ResourceAllocation(Base):
    """Immutable resource/identity snapshot for each container runtime session."""
    __tablename__='resource_allocations'
    __table_args__=(UniqueConstraint('node_id','runtime_id'),)
    id=mapped_column(String,primary_key=True,default=uid)
    workspace_id=mapped_column(String,index=True)
    node_id=mapped_column(String,index=True)
    runtime_id=mapped_column(String)
    owner_id=mapped_column(String,index=True)
    project_id=mapped_column(String,index=True)
    started_at=mapped_column(Float,index=True)
    ended_at=mapped_column(Float,nullable=True,index=True)
    start_source=mapped_column(String)
    end_reason=mapped_column(String,default='')
    snapshot=mapped_column(JSON,default=dict)

class ResourceMetric(Base):
    """One compact node sample per minute; no process commands or credentials."""
    __tablename__='resource_metrics'
    __table_args__=(UniqueConstraint('node_id','bucket'),)
    id=mapped_column(String,primary_key=True,default=uid)
    node_id=mapped_column(String,index=True)
    bucket=mapped_column(Integer,index=True)
    collected_at=mapped_column(Float,index=True)
    payload=mapped_column(JSON,default=dict)

engine=create_engine(os.environ.get('DATABASE_URL','sqlite:///test.db'),pool_pre_ping=True)
Session=sessionmaker(engine,expire_on_commit=False)

def record(db,user,action,obj='',project=None,detail=None):
    db.add(Audit(actor_id=user.id,actor=user.username,action=action,object_id=obj,project_id=project,detail=detail or {}))

def data(obj):
    return {c.name:getattr(obj,c.name) for c in obj.__table__.columns}
