from faker import Faker

fake = Faker()

def generate_person():
    return {
        'first_name': fake.first_name(),
        'last_name': fake.last_name(),
        'dob': fake.date_of_birth(),
        'city': fake.city(),
        'state': fake.state(),
        'zipcode': fake.zipcode()
    }

def generate_name():
    return fake.catch_phrase()


def generate_text():
    return fake.text()
