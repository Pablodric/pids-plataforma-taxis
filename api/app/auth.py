"""
Autenticacion y autorizacion (E7).

Regla de oro del escenario multiempresa: la identidad de la empresa
NUNCA se deduce del texto que escribe el usuario en el chat. Sale de un
token firmado que se valida aqui, antes de que el mensaje llegue al
motor de dialogo. Asi, aunque alguien escriba "dame los datos de
Movilidad Sur", el modelo no tiene forma de cambiar de empresa.
"""

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app import config, db

esquema_bearer = HTTPBearer(auto_error=False)

# Permisos EXPLICITOS por rol, no una jerarquia lineal.
# La primera version usaba usuario < operador < auditor, y eso daba al
# auditor el permiso de escritura del operador: podia corregir viajes de
# cualquier empresa. Quien audita no debe poder alterar lo auditado, asi
# que el auditor lee todo pero no escribe nada.
PERMISOS_POR_ROL = {
    "usuario":  frozenset({"consultar"}),
    "operador": frozenset({"consultar", "corregir"}),
    "auditor":  frozenset({"consultar", "ver_global"}),
}
PERMISOS = frozenset().union(*PERMISOS_POR_ROL.values())


@dataclass
class Sesion:
    email: str
    empresa_id: str
    rol: str

    def puede(self, permiso: str) -> bool:
        if permiso not in PERMISOS:
            raise ValueError(f"Permiso desconocido: {permiso}")
        return permiso in PERMISOS_POR_ROL.get(self.rol, frozenset())


def verificar_password(password: str, almacenado: str) -> bool:
    """Compara en tiempo constante contra el hash pbkdf2$<sal>$<hash>."""
    try:
        etiqueta, salt, esperado = almacenado.split("$", 2)
    except ValueError:
        return False
    if etiqueta != "pbkdf2":
        return False
    calculado = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), 200_000
    ).hex()
    return hmac.compare_digest(calculado, esperado)


def autenticar(email: str, password: str) -> Sesion | None:
    with db.conexion_sin_contexto() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT email, empresa_id, rol, password_hash, activo "
            "FROM usuarios WHERE email = %s",
            (email.lower().strip(),),
        )
        fila = cur.fetchone()

    if fila is None or not fila["activo"]:
        return None
    if not verificar_password(password, fila["password_hash"]):
        return None
    return Sesion(email=fila["email"], empresa_id=fila["empresa_id"], rol=fila["rol"])


def emitir_token(sesion: Sesion) -> tuple[str, int]:
    expira_en = config.JWT_MINUTOS * 60
    carga = {
        "sub": sesion.email,
        "empresa_id": sesion.empresa_id,
        "rol": sesion.rol,
        "exp": datetime.now(UTC) + timedelta(seconds=expira_en),
        "iat": datetime.now(UTC),
    }
    token = jwt.encode(carga, config.JWT_SECRETO, algorithm=config.JWT_ALGORITMO)
    return token, expira_en


def sesion_actual(
    credenciales: HTTPAuthorizationCredentials | None = Depends(esquema_bearer),
) -> Sesion:
    """Dependencia de FastAPI: corta la peticion si el token no es valido."""
    if credenciales is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Falta el token de sesion",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        carga = jwt.decode(
            credenciales.credentials,
            config.JWT_SECRETO,
            algorithms=[config.JWT_ALGORITMO],
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="La sesion ha caducado") from None
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token no valido") from None

    try:
        return Sesion(
            email=carga["sub"], empresa_id=carga["empresa_id"], rol=carga["rol"]
        )
    except KeyError:
        raise HTTPException(status_code=401, detail="Token no valido") from None


DESCRIPCION_PERMISO = {
    "consultar": "consultar datos",
    "corregir": "registrar correcciones (rol operador)",
    "ver_global": "ver metricas globales (rol auditor)",
}


def exigir_permiso(permiso: str):
    """Uso: Depends(exigir_permiso('corregir'))."""
    if permiso not in PERMISOS:
        raise ValueError(f"Permiso desconocido: {permiso}")

    def dependencia(sesion: Sesion = Depends(sesion_actual)) -> Sesion:
        if not sesion.puede(permiso):
            from app import repositorio  # import diferido: evita el ciclo
            repositorio.registrar_auditoria(
                sesion.email, sesion.empresa_id, "permiso_denegado", permiso, False
            )
            raise HTTPException(
                status_code=403,
                detail=f"Tu rol ({sesion.rol}) no permite {DESCRIPCION_PERMISO[permiso]}",
            )
        return sesion

    return dependencia
