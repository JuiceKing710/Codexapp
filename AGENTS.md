{\rtf1\ansi\ansicpg1252\cocoartf2868
\cocoatextscaling0\cocoaplatform0{\fonttbl\f0\fswiss\fcharset0 Helvetica;}
{\colortbl;\red255\green255\blue255;}
{\*\expandedcolortbl;;}
\margl1440\margr1440\vieww11520\viewh8400\viewkind0
\pard\tx720\tx1440\tx2160\tx2880\tx3600\tx4320\tx5040\tx5760\tx6480\tx7200\tx7920\tx8640\pardirnatural\partightenfactor0

\f0\fs24 \cf0 # Project rules\
\
## Safety\
- This project is read-only.\
- Never add undocumented control commands.\
- Never add frame replay, security access, coding, reflashing, or programming.\
- Never assume CAN for DLC3 engine diagnostics on the 2006 Toyota Sienna unless the source explicitly indicates CAN capture.\
\
## Vehicle context\
- Vehicle: 2006 Toyota Sienna FWD V6 3.3L (3MZ-FE)\
- DLC3/ECM diagnostics should first be treated as ISO 9141-2 request/response traffic.\
- Internal vehicle networks may include Toyota CAN and other multiplex systems.\
\
## Build priorities\
1. Stable project structure\
2. Read-only adapter layer\
3. Logging and preprocessing\
4. FastAPI dashboard\
5. LM Studio local review\
6. OpenAI second-opinion review\
7. Documentation\
\
## Code style\
- Keep modules small and readable\
- Prefer explicit JSON schemas\
- Add comments where hardware/protocol assumptions matter\
- Fail closed on anything unsafe}