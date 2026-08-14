from datetime import datetime
from enum import Enum
from sqlalchemy import String,Integer,Float,JSON,DateTime
from sqlalchemy.orm import Mapped,mapped_column
from base import Base
class LeadStatus(str,Enum):
    NEW="NEW"; ANALYZED="ANALYZED"; QUALIFIED="QUALIFIED"; CONTACTED="CONTACTED"; CLIENT="CLIENT"; NO_WEBSITE="NO_WEBSITE"
class Lead(Base):
    __tablename__="leads"
    id:Mapped[int]=mapped_column(Integer,primary_key=True)
    source_id:Mapped[str]=mapped_column(String(120),unique=True,index=True)
    source:Mapped[str]=mapped_column(String(40),default="openstreetmap")
    name:Mapped[str]=mapped_column(String(255))
    category:Mapped[str|None]=mapped_column(String(120))
    website:Mapped[str|None]=mapped_column(String(500))
    phone:Mapped[str|None]=mapped_column(String(80))
    address:Mapped[str|None]=mapped_column(String(500))
    city:Mapped[str|None]=mapped_column(String(120))
    country:Mapped[str|None]=mapped_column(String(120))
    latitude:Mapped[float|None]=mapped_column(Float)
    longitude:Mapped[float|None]=mapped_column(Float)
    website_score:Mapped[float|None]=mapped_column(Float)
    lead_score:Mapped[float|None]=mapped_column(Float)
    opportunity:Mapped[str|None]=mapped_column(String(255))
    problems:Mapped[list|None]=mapped_column(JSON)
    analysis:Mapped[dict|None]=mapped_column(JSON)
    status:Mapped[LeadStatus]=mapped_column(default=LeadStatus.NEW)
    created_at:Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)
    def to_dict(self,full=False):
        d={c.name:getattr(self,c.name) for c in self.__table__.columns}
        d["status"]=self.status.value; d["created_at"]=self.created_at.isoformat()
        if not full:d.pop("analysis",None)
        return d

class Campaign(Base):
    __tablename__="campaigns"
    id:Mapped[int]=mapped_column(Integer,primary_key=True)
    niche:Mapped[str]=mapped_column(String(80))
    city:Mapped[str]=mapped_column(String(120))
    leads_per_day:Mapped[int]=mapped_column(Integer)
    total_days:Mapped[int]=mapped_column(Integer)
    days_completed:Mapped[int]=mapped_column(Integer,default=0)
    next_run:Mapped[datetime]=mapped_column(DateTime,index=True)
    status:Mapped[str]=mapped_column(String(20),default="ACTIVE",index=True)
    created_at:Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)
    last_run_at:Mapped[datetime|None]=mapped_column(DateTime)
    last_error:Mapped[str|None]=mapped_column(String(500))
    telegram_update_id:Mapped[int|None]=mapped_column(Integer,unique=True,index=True)
    
