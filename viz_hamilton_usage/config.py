import os


def db_config():
    return {
        "host": os.getenv("QC_DB_HOST", "192.168.60.4"),
        "port": int(os.getenv("QC_DB_PORT", "3307")),
        "user": os.getenv("QC_DB_USER", "labsite"),
        "password": os.getenv("QC_DB_PASSWORD", "vibrant"),
    }


TEST_MACHINES = {"H14", "H13", "H7"}

# Optional aliases to display alongside canonical IDs
H_ALIAS_NAMES = {
    "H3": ["ELISA_HAMILTON_1"],
    "H4": ["ELISA_HAMILTON_2"],
}
