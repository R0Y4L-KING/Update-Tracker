import sqlite3


db = sqlite3.connect(
    "bot_database.db",
    check_same_thread=False
)

cursor = db.cursor()


cursor.execute("""
CREATE TABLE IF NOT EXISTS apps(

id INTEGER PRIMARY KEY AUTOINCREMENT,

app_name TEXT,

channel_id INTEGER,

old_message_id INTEGER,

update_message_id INTEGER DEFAULT 0,

expiry_date TEXT

)
""")


db.commit()



def add_app(
    app_name,
    channel_id,
    message_id,
    expiry
):

    cursor.execute(
        """
        INSERT INTO apps
        (
        app_name,
        channel_id,
        old_message_id,
        expiry_date
        )

        VALUES(?,?,?,?)
        """,

        (
        app_name,
        channel_id,
        message_id,
        expiry
        )
    )


    db.commit()



def add_update(
    app_name,
    message_id
):

    cursor.execute(
        """
        UPDATE apps

        SET update_message_id=?

        WHERE app_name=?
        """,

        (
        message_id,
        app_name
        )
    )


    db.commit()



def get_expiring(date):

    cursor.execute(
        """
        SELECT *
        FROM apps
        WHERE expiry_date=?
        """,

        (date,)
    )


    return cursor.fetchall()



def get_update(app_name):

    cursor.execute(
        """
        SELECT update_message_id
        FROM apps
        WHERE app_name=?
        """,

        (app_name,)
    )


    result = cursor.fetchone()


    if result:
        return result[0]


    return 0