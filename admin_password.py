import bcrypt
password = "bruh"
hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
print(f"Hashed password: {hashed}")


import bcrypt

username = "pokedb"  # Replace with your username
password = "WwAaSsDd@1999"  # Replace with your password
hashed = "$2b$12$3JYMACmzg.434zVSnHwGHuPo10dZPTnB1BGdLCjbmhC9JstD/t2xu"  # Replace with the hash from users.json
if bcrypt.checkpw(password.encode(), hashed.encode()):
    print("Credentials are valid")
else:
    print("Credentials are invalid")