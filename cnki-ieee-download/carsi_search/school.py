"""学校 / 机构配置 — 换学校只改这里（或用环境变量覆盖）。

本仓库原作者基于 **西安电子科技大学** 调试，这里改为 **北京理工大学**。

entityId 的查法（IEEE 官方接口，浏览器里直接打开即可）：
    https://ieeexplore.ieee.org/rest/api/auth/wayf-by-displayname?keyword=<学校英文名>&url=/Xplore/home.jsp

北京理工大学的返回值：
    Beijing Institute of Technology(OpenAthens) -> https://idp.bit.edu.cn/entity
    Beijing Institute of Technology             -> https://idp.bit.edu.cn/idp/shibboleth   ← Shibboleth/CARSI
"""

import os

# CNKI「校外访问」机构列表里要选的学校名（中文全称，需与 fsso.cnki.net 列表一致）
SCHOOL_NAME = os.environ.get("CARSI_SCHOOL_NAME", "北京理工大学")

# 学校 Shibboleth IdP 域名，用于判断是否已跳到本校认证页
SCHOOL_IDP_HOST = os.environ.get("CARSI_IDP_HOST", "idp.bit.edu.cn")

# IEEE 机构登录用的 entityId（Shibboleth/CARSI）
IEEE_ENTITY_ID = os.environ.get(
    "CARSI_IEEE_ENTITY_ID", "https://idp.bit.edu.cn/idp/shibboleth"
)

# ScienceDirect(Elsevier) 机构登录用的 entityId，通常与上面相同
SD_ENTITY_ID = os.environ.get("CARSI_SD_ENTITY_ID", IEEE_ENTITY_ID)
