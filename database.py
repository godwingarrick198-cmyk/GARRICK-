import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from base import Base
URL=os.getenv("DATABASE_URL","sqlite:///./garrick.db")
engine=create_engine(URL,connect_args={"check_same_thread":False} if URL.startswith("sqlite") else {})
SessionLocal=sessionmaker(bind=engine)
def init_db():
    import models
    Base.metadata.create_all(engine)
