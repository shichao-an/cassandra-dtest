import time

from cassandra import AuthenticationFailed, Unauthorized
from cassandra.cluster import NoHostAvailable
from cassandra.protocol import SyntaxException
from auth_test import data_resource_creator_permissions, role_creator_permissions, function_resource_creator_permissions
from dtest import debug,Tester
from assertions import *
from tools import since

#Second value is superuser status
#Third value is login status, See #7653 for explanation.
mike_role = ['mike', False, True, {}]
role1_role = ['role1', False, False, {}]
role2_role = ['role2', False, False, {}]
cassandra_role = ['cassandra', True, True, {}]


@since('3.0')
class TestAuthRoles(Tester):

    def create_drop_role_test(self):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        assert_one(cassandra, 'LIST ROLES', cassandra_role)

        cassandra.execute("CREATE ROLE role1")
        assert_all(cassandra, "LIST ROLES", [cassandra_role, role1_role])

        cassandra.execute("DROP ROLE role1")
        assert_one(cassandra, "LIST ROLES", cassandra_role)

    def conditional_create_drop_role_test(self):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        assert_one(cassandra, "LIST ROLES", cassandra_role)

        cassandra.execute("CREATE ROLE IF NOT EXISTS role1")
        cassandra.execute("CREATE ROLE IF NOT EXISTS role1")
        assert_all(cassandra, "LIST ROLES", [cassandra_role, role1_role])

        cassandra.execute("DROP ROLE IF EXISTS role1")
        cassandra.execute("DROP ROLE IF EXISTS role1")
        assert_one(cassandra, "LIST ROLES", cassandra_role)

    def create_drop_role_validation_test(self):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        cassandra.execute("CREATE ROLE mike WITH PASSWORD = '12345' AND SUPERUSER = false AND LOGIN = true")
        mike = self.get_session(user='mike', password='12345')

        assert_invalid(mike,
                       "CREATE ROLE role2",
                       "User mike does not have sufficient privileges to perform the requested operation",
                       Unauthorized)
        cassandra.execute("CREATE ROLE role1")

        assert_invalid(mike,
                       "DROP ROLE role1",
                       "User mike does not have sufficient privileges to perform the requested operation",
                       Unauthorized)

        assert_invalid(cassandra, "CREATE ROLE role1", "role1 already exists")
        cassandra.execute("DROP ROLE role1")
        assert_invalid(cassandra, "DROP ROLE role1", "role1 doesn't exist")

    def role_admin_validation_test(self):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        cassandra.execute("CREATE ROLE administrator WITH SUPERUSER = false AND LOGIN = false")
        cassandra.execute("GRANT ALL ON ALL ROLES TO administrator")
        cassandra.execute("CREATE ROLE mike WITH PASSWORD = '12345' AND SUPERUSER = false AND LOGIN = true")
        cassandra.execute("GRANT administrator TO mike")
        cassandra.execute("CREATE ROLE klaus WITH PASSWORD = '54321' AND SUPERUSER = false AND LOGIN = true")
        mike = self.get_session(user='mike', password='12345')
        klaus = self.get_session(user='klaus', password='54321')

        # roles with CREATE on ALL ROLES can create roles
        mike.execute("CREATE ROLE role1 WITH PASSWORD = '11111' AND LOGIN = false")

        # require ALTER on ALL ROLES or a SPECIFIC ROLE to modify
        self.assert_unauthenticated('role1 is not permitted to log in', 'role1', '11111')
        cassandra.execute("GRANT ALTER on ROLE role1 TO klaus")
        klaus.execute("ALTER ROLE role1 WITH LOGIN = true")
        mike.execute("ALTER ROLE role1 WITH PASSWORD = '22222'")
        role1 = self.get_session(user='role1', password='22222')

        # only superusers can set superuser status
        assert_invalid(mike, "ALTER ROLE role1 WITH SUPERUSER = true",
                       "Only superusers are allowed to alter superuser status",
                       Unauthorized)
        assert_invalid(mike, "ALTER ROLE mike WITH SUPERUSER = true",
                       "You aren't allowed to alter your own superuser status or that of a role granted to you",
                       Unauthorized)

        # roles without necessary permissions cannot create, drop or alter roles except themselves
        assert_invalid(role1, "CREATE ROLE role2 WITH LOGIN = false",
                       "User role1 does not have sufficient privileges to perform the requested operation",
                       Unauthorized)
        assert_invalid(role1, "ALTER ROLE mike WITH LOGIN = false",
                       "User role1 does not have sufficient privileges to perform the requested operation",
                       Unauthorized)
        assert_invalid(role1, "DROP ROLE mike",
                       "User role1 does not have sufficient privileges to perform the requested operation",
                       Unauthorized)
        role1.execute("ALTER ROLE role1 WITH PASSWORD = '33333'")

        # roles with roleadmin can drop roles
        mike.execute("DROP ROLE role1")
        assert_all(cassandra, "LIST ROLES", [['administrator', False, False, {}],
                                             cassandra_role,
                                             ['klaus', False, True, {}],
                                             mike_role])

        # revoking role admin removes its privileges
        cassandra.execute("REVOKE administrator FROM mike")
        assert_invalid(mike, "CREATE ROLE role3 WITH LOGIN = false",
                       "User mike does not have sufficient privileges to perform the requested operation",
                       Unauthorized)

    def creator_of_db_resource_granted_all_permissions_test(self):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        cassandra.execute("CREATE ROLE mike WITH PASSWORD = '12345' AND SUPERUSER = false AND LOGIN = true")
        cassandra.execute("GRANT CREATE ON ALL KEYSPACES TO mike")
        cassandra.execute("GRANT CREATE ON ALL ROLES TO mike")

        mike = self.get_session(user='mike', password='12345')
        # mike should automatically be granted permissions on any resource he creates, i.e. tables or roles
        mike.execute("CREATE KEYSPACE ks WITH replication = {'class':'SimpleStrategy', 'replication_factor':1}")
        mike.execute("CREATE TABLE ks.cf (id int primary key, val int)")
        mike.execute("CREATE ROLE role1 WITH PASSWORD = '11111' AND SUPERUSER = false AND LOGIN = true")
        mike.execute("""CREATE FUNCTION ks.state_function_1(a int, b int)
                        RETURNS int
                        LANGUAGE javascript
                        AS ' a + b'""")
        mike.execute("""CREATE AGGREGATE ks.simple_aggregate_1(int)
                        SFUNC state_function_1
                        STYPE int
                        INITCOND 0""")

        cassandra_permissions = role_creator_permissions('cassandra', '<role mike>')
        mike_permissions = [('mike', '<all roles>', 'CREATE'),
                            ('mike', '<all keyspaces>', 'CREATE')]
        mike_permissions.extend(role_creator_permissions('mike', '<role role1>'))
        mike_permissions.extend(data_resource_creator_permissions('mike', '<keyspace ks>'))
        mike_permissions.extend(data_resource_creator_permissions('mike', '<table ks.cf>'))
        mike_permissions.extend(function_resource_creator_permissions('mike', '<function ks.state_function_1(int, int)>'))
        mike_permissions.extend(function_resource_creator_permissions('mike', '<function ks.simple_aggregate_1(int)>'))

        self.assert_permissions_listed(cassandra_permissions + mike_permissions,
                                       cassandra,
                                       "LIST ALL PERMISSIONS")

    def create_and_grant_roles_with_superuser_status_test(self):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        cassandra.execute("CREATE ROLE another_superuser WITH SUPERUSER = true AND LOGIN = false")
        cassandra.execute("CREATE ROLE non_superuser WITH SUPERUSER = false AND LOGIN = false")
        cassandra.execute("CREATE ROLE mike WITH PASSWORD = '12345' AND SUPERUSER = false AND LOGIN = true")
        # mike can create and grant any role, except superusers
        cassandra.execute("GRANT CREATE ON ALL ROLES TO mike")
        cassandra.execute("GRANT AUTHORIZE ON ALL ROLES TO mike")

        # mike can create roles, but not with superuser status
        # and can grant any role, including those with superuser status
        mike = self.get_session(user='mike', password='12345')
        mike.execute("CREATE ROLE role1 WITH SUPERUSER = false")
        mike.execute("GRANT non_superuser TO role1")
        mike.execute("GRANT another_superuser TO role1")
        assert_invalid(mike, "CREATE ROLE role2 WITH SUPERUSER = true",
                       "Only superusers can create a role with superuser status",
                       Unauthorized)
        assert_all(cassandra, "LIST ROLES OF role1", [['another_superuser', True, False, {}],
                                                      ['non_superuser', False, False, {}],
                                                      ['role1', False, False, {}]])

    def drop_and_revoke_roles_with_superuser_status_test(self):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        cassandra.execute("CREATE ROLE another_superuser WITH SUPERUSER = true AND LOGIN = false")
        cassandra.execute("CREATE ROLE non_superuser WITH SUPERUSER = false AND LOGIN = false")
        cassandra.execute("CREATE ROLE role1 WITH SUPERUSER = false")
        cassandra.execute("GRANT another_superuser TO role1")
        cassandra.execute("GRANT non_superuser TO role1")
        cassandra.execute("CREATE ROLE mike WITH PASSWORD = '12345' AND SUPERUSER = false AND LOGIN = true")
        cassandra.execute("GRANT DROP ON ALL ROLES TO mike")
        cassandra.execute("GRANT AUTHORIZE ON ALL ROLES TO mike")

        # mike can drop and revoke any role, including superusers
        mike = self.get_session(user='mike', password='12345')
        mike.execute("REVOKE another_superuser FROM role1")
        mike.execute("REVOKE non_superuser FROM role1")
        mike.execute("DROP ROLE non_superuser")
        mike.execute("DROP ROLE role1")


    def drop_role_removes_memberships_test(self):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        cassandra.execute("CREATE ROLE role1")
        cassandra.execute("CREATE ROLE role2")
        cassandra.execute("CREATE ROLE mike WITH PASSWORD = '12345' AND SUPERUSER = false AND LOGIN = true")
        cassandra.execute("GRANT role2 TO role1")
        cassandra.execute("GRANT role1 TO mike")
        assert_all(cassandra, "LIST ROLES OF mike", [mike_role, role1_role, role2_role])

        # drop the role indirectly granted
        cassandra.execute("DROP ROLE role2")
        assert_all(cassandra, "LIST ROLES OF mike", [mike_role, role1_role])

        cassandra.execute("CREATE ROLE role2")
        cassandra.execute("GRANT role2 to role1")
        assert_all(cassandra, "LIST ROLES OF mike", [mike_role, role1_role, role2_role])
        # drop the directly granted role
        cassandra.execute("DROP ROLE role1")
        assert_one(cassandra, "LIST ROLES OF mike", mike_role)
        assert_all(cassandra, "LIST ROLES", [cassandra_role, mike_role, role2_role])

    def drop_role_revokes_permissions_granted_on_it_test(self):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        cassandra.execute("CREATE ROLE role1")
        cassandra.execute("CREATE ROLE role2")
        cassandra.execute("CREATE ROLE mike WITH PASSWORD = '12345' AND SUPERUSER = false AND LOGIN = true")
        cassandra.execute("GRANT ALTER ON ROLE role1 TO mike")
        cassandra.execute("GRANT AUTHORIZE ON ROLE role2 TO mike")

        self.assert_permissions_listed([("mike", "<role role1>", "ALTER"),
                                        ("mike", "<role role2>", "AUTHORIZE")],
                                       cassandra,
                                       "LIST ALL PERMISSIONS OF mike")

        cassandra.execute("DROP ROLE role1")
        cassandra.execute("DROP ROLE role2")
        assert cassandra.execute("LIST ALL PERMISSIONS OF mike") is None

    def grant_revoke_roles_test(self):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        cassandra.execute("CREATE ROLE mike WITH PASSWORD = '12345' AND SUPERUSER = false AND LOGIN = true")
        cassandra.execute("CREATE ROLE role1")
        cassandra.execute("CREATE ROLE role2")
        cassandra.execute("GRANT role1 TO role2")
        cassandra.execute("GRANT role2 TO mike")

        assert_all(cassandra, "LIST ROLES OF role2", [role1_role, role2_role])
        assert_all(cassandra, "LIST ROLES OF mike", [mike_role, role1_role, role2_role])
        assert_all(cassandra, "LIST ROLES OF mike NORECURSIVE", [mike_role, role2_role])

        cassandra.execute("REVOKE role2 FROM mike")
        assert_one(cassandra, "LIST ROLES OF mike", mike_role)

        cassandra.execute("REVOKE role1 FROM role2")
        assert_one(cassandra, "LIST ROLES OF role2", role2_role)

    def grant_revoke_role_validation_test(self):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        cassandra.execute("CREATE ROLE mike WITH PASSWORD = '12345' AND SUPERUSER = false AND LOGIN = true")
        mike = self.get_session(user='mike', password='12345')

        assert_invalid(cassandra, "GRANT role1 TO mike", "role1 doesn't exist")
        cassandra.execute("CREATE ROLE role1")

        assert_invalid(cassandra, "GRANT role1 TO john", "john doesn't exist")
        assert_invalid(cassandra, "GRANT role2 TO john", "role2 doesn't exist")

        cassandra.execute("CREATE ROLE john WITH PASSWORD = '12345' AND SUPERUSER = false AND LOGIN = true")
        cassandra.execute("CREATE ROLE role2")

        assert_invalid(mike,
                       "GRANT role2 TO john",
                       "User mike does not have sufficient privileges to perform the requested operation",
                       Unauthorized)

        # superusers can always grant roles
        cassandra.execute("GRANT role1 TO john")
        # but regular users need AUTHORIZE permission on the granted role
        cassandra.execute("GRANT AUTHORIZE ON ROLE role2 TO mike")
        mike.execute("GRANT role2 TO john")

        # same applies to REVOKEing roles
        assert_invalid(mike,
                       "REVOKE role1 FROM john",
                       "User mike does not have sufficient privileges to perform the requested operation",
                       Unauthorized)
        cassandra.execute("REVOKE role1 FROM john")
        mike.execute("REVOKE role2 from john")

    def list_roles_test(self):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        cassandra.execute("CREATE ROLE mike WITH PASSWORD = '12345' AND SUPERUSER = false AND LOGIN = true")
        cassandra.execute("CREATE ROLE role1")
        cassandra.execute("CREATE ROLE role2")

        assert_all(cassandra, "LIST ROLES", [cassandra_role, mike_role, role1_role, role2_role])

        cassandra.execute("GRANT role1 TO role2")
        cassandra.execute("GRANT role2 TO mike")

        assert_all(cassandra, "LIST ROLES OF role2", [role1_role, role2_role])
        assert_all(cassandra, "LIST ROLES OF mike", [mike_role, role1_role, role2_role])
        assert_all(cassandra, "LIST ROLES OF mike NORECURSIVE", [mike_role, role2_role])

        mike = self.get_session(user='mike', password='12345')
        assert_invalid(mike,
                       "LIST ROLES OF cassandra",
                       "You are not authorized to view roles granted to cassandra",
                       Unauthorized)

        assert_all(mike, "LIST ROLES", [mike_role, role1_role, role2_role])
        assert_all(mike, "LIST ROLES OF mike", [mike_role, role1_role, role2_role])
        assert_all(mike, "LIST ROLES OF mike NORECURSIVE", [mike_role, role2_role])
        assert_all(mike, "LIST ROLES OF role2", [role1_role, role2_role])

        # without SELECT permission on the root level roles resource, LIST ROLES with no OF
        # returns only the roles granted to the user. With it, it includes all roles.
        assert_all(mike, "LIST ROLES", [mike_role, role1_role, role2_role])
        cassandra.execute("GRANT DESCRIBE ON ALL ROLES TO mike")
        assert_all(mike, "LIST ROLES", [cassandra_role, mike_role, role1_role, role2_role])


    def grant_revoke_permissions_test(self):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        cassandra.execute("CREATE KEYSPACE ks WITH replication = {'class':'SimpleStrategy', 'replication_factor':1}")
        cassandra.execute("CREATE TABLE ks.cf (id int primary key, val int)")
        cassandra.execute("CREATE ROLE mike WITH PASSWORD = '12345' AND SUPERUSER = false AND LOGIN = true")
        cassandra.execute("CREATE ROLE role1")
        cassandra.execute("GRANT ALL ON table ks.cf TO role1")
        cassandra.execute("GRANT role1 TO mike")

        mike = self.get_session(user='mike', password='12345')
        mike.execute("INSERT INTO ks.cf (id, val) VALUES (0, 0)")

        assert_one(mike, "SELECT * FROM ks.cf", [0, 0])

        cassandra.execute("REVOKE role1 FROM mike")
        assert_invalid(mike,
                       "INSERT INTO ks.cf (id, val) VALUES (0, 0)",
                       "mike has no MODIFY permission on <table ks.cf> or any of its parents",
                       Unauthorized)

        cassandra.execute("GRANT role1 TO mike")
        cassandra.execute("REVOKE ALL ON ks.cf FROM role1")

        assert_invalid(mike,
                       "INSERT INTO ks.cf (id, val) VALUES (0, 0)",
                       "mike has no MODIFY permission on <table ks.cf> or any of its parents",
                       Unauthorized)

    def filter_granted_permissions_by_resource_type_test(self):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        cassandra.execute("CREATE KEYSPACE ks WITH replication = {'class':'SimpleStrategy', 'replication_factor':1}")
        cassandra.execute("CREATE TABLE ks.cf (id int primary key, val int)")
        cassandra.execute("CREATE ROLE mike WITH PASSWORD = '12345' AND SUPERUSER = false AND LOGIN = true")
        cassandra.execute("CREATE ROLE role1 WITH SUPERUSER = false AND LOGIN = false")
        cassandra.execute("CREATE FUNCTION ks.state_func(a int, b int) RETURNS int LANGUAGE javascript AS 'a+b'")
        cassandra.execute("CREATE AGGREGATE ks.agg_func(int) SFUNC state_func STYPE int")

        # GRANT ALL ON ALL KEYSPACES grants Permission.ALL_DATA

        # GRANT ALL ON KEYSPACE grants Permission.ALL_DATA
        cassandra.execute("GRANT ALL ON KEYSPACE ks TO mike")
        self.assert_permissions_listed([("mike", "<keyspace ks>", "CREATE"),
                                        ("mike", "<keyspace ks>", "ALTER"),
                                        ("mike", "<keyspace ks>", "DROP"),
                                        ("mike", "<keyspace ks>", "SELECT"),
                                        ("mike", "<keyspace ks>", "MODIFY"),
                                        ("mike", "<keyspace ks>", "AUTHORIZE")],
                                       cassandra,
                                       "LIST ALL PERMISSIONS OF mike")
        cassandra.execute("REVOKE ALL ON KEYSPACE ks FROM mike")

        # GRANT ALL ON TABLE does not include CREATE (because the table must already be created before the GRANT)
        cassandra.execute("GRANT ALL ON ks.cf TO MIKE")
        self.assert_permissions_listed([("mike", "<table ks.cf>", "ALTER"),
                                        ("mike", "<table ks.cf>", "DROP"),
                                        ("mike", "<table ks.cf>", "SELECT"),
                                        ("mike", "<table ks.cf>", "MODIFY"),
                                        ("mike", "<table ks.cf>", "AUTHORIZE")],
                                       cassandra,
                                       "LIST ALL PERMISSIONS OF mike")
        cassandra.execute("REVOKE ALL ON ks.cf FROM mike")
        assert_invalid(cassandra,
                       "GRANT CREATE ON ks.cf TO MIKE",
                       "Resource type DataResource does not support any of the requested permissions",
                       SyntaxException)

        # GRANT ALL ON ALL ROLES includes SELECT & CREATE on the root level roles resource
        cassandra.execute("GRANT ALL ON ALL ROLES TO mike")
        self.assert_permissions_listed([("mike", "<all roles>", "CREATE"),
                                        ("mike", "<all roles>", "ALTER"),
                                        ("mike", "<all roles>", "DROP"),
                                        ("mike", "<all roles>", "DESCRIBE"),
                                        ("mike", "<all roles>", "AUTHORIZE")],
                                       cassandra,
                                       "LIST ALL PERMISSIONS OF mike")
        cassandra.execute("REVOKE ALL ON ALL ROLES FROM mike")
        assert_invalid(cassandra,
                       "GRANT SELECT ON ALL ROLES TO MIKE",
                       "Resource type RoleResource does not support any of the requested permissions",
                       SyntaxException)

        # GRANT ALL ON ROLE does not include CREATE (because the role must already be created before the GRANT)
        cassandra.execute("GRANT ALL ON ROLE role1 TO mike")
        self.assert_permissions_listed([("mike", "<role role1>", "ALTER"),
                                        ("mike", "<role role1>", "DROP"),
                                        ("mike", "<role role1>", "AUTHORIZE")],
                                       cassandra,
                                       "LIST ALL PERMISSIONS OF mike")
        assert_invalid(cassandra,
                       "GRANT CREATE ON ROLE role1 TO MIKE",
                       "Resource type RoleResource does not support any of the requested permissions",
                       SyntaxException)
        cassandra.execute("REVOKE ALL ON ROLE role1 FROM mike")

        # GRANT ALL ON ALL FUNCTIONS or on all functions for a single keyspace includes AUTHORIZE, EXECUTE and USAGE
        cassandra.execute("GRANT ALL ON ALL FUNCTIONS TO mike")
        self.assert_permissions_listed([("mike", "<all functions>", "CREATE"),
                                        ("mike", "<all functions>", "ALTER"),
                                        ("mike", "<all functions>", "DROP"),
                                        ("mike", "<all functions>", "AUTHORIZE"),
                                        ("mike", "<all functions>", "EXECUTE")],
                                       cassandra,
                                       "LIST ALL PERMISSIONS OF mike")
        cassandra.execute("REVOKE ALL ON ALL FUNCTIONS FROM mike")

        cassandra.execute("GRANT ALL ON ALL FUNCTIONS IN KEYSPACE ks TO mike")
        self.assert_permissions_listed([("mike", "<all functions in ks>", "CREATE"),
                                        ("mike", "<all functions in ks>", "ALTER"),
                                        ("mike", "<all functions in ks>", "DROP"),
                                        ("mike", "<all functions in ks>", "AUTHORIZE"),
                                        ("mike", "<all functions in ks>", "EXECUTE")],
                                       cassandra,
                                       "LIST ALL PERMISSIONS OF mike")
        cassandra.execute("REVOKE ALL ON ALL FUNCTIONS IN KEYSPACE ks FROM mike")

        # GRANT ALL ON FUNCTION includes AUTHORIZE, EXECUTE and USAGE for scalar functions and
        # AUTHORIZE and EXECUTE for aggregates
        cassandra.execute("GRANT ALL ON FUNCTION ks.state_func(int, int) TO mike")
        self.assert_permissions_listed([("mike", "<function ks.state_func(int, int)>", "ALTER"),
                                        ("mike", "<function ks.state_func(int, int)>", "DROP"),
                                        ("mike", "<function ks.state_func(int, int)>", "AUTHORIZE"),
                                        ("mike", "<function ks.state_func(int, int)>", "EXECUTE")],
                                       cassandra,
                                       "LIST ALL PERMISSIONS OF mike")
        cassandra.execute("REVOKE ALL ON FUNCTION ks.state_func(int, int) FROM mike")

        cassandra.execute("GRANT ALL ON FUNCTION ks.agg_func(int) TO mike")
        self.assert_permissions_listed([("mike", "<function ks.agg_func(int)>", "ALTER"),
                                        ("mike", "<function ks.agg_func(int)>", "DROP"),
                                        ("mike", "<function ks.agg_func(int)>", "AUTHORIZE"),
                                        ("mike", "<function ks.agg_func(int)>", "EXECUTE")],
                                       cassandra,
                                       "LIST ALL PERMISSIONS OF mike")
        cassandra.execute("REVOKE ALL ON FUNCTION ks.agg_func(int) FROM mike")

    def list_permissions_test(self):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        cassandra.execute("CREATE KEYSPACE ks WITH replication = {'class':'SimpleStrategy', 'replication_factor':1}")
        cassandra.execute("CREATE TABLE ks.cf (id int primary key, val int)")
        cassandra.execute("CREATE ROLE mike WITH PASSWORD = '12345' AND SUPERUSER = false AND LOGIN = true")
        cassandra.execute("CREATE ROLE role1")
        cassandra.execute("CREATE ROLE role2")
        cassandra.execute("GRANT SELECT ON table ks.cf TO role1")
        cassandra.execute("GRANT ALTER ON table ks.cf TO role2")
        cassandra.execute("GRANT MODIFY ON table ks.cf TO mike")
        cassandra.execute("GRANT ALTER ON ROLE role1 TO role2")
        cassandra.execute("GRANT role1 TO role2")
        cassandra.execute("GRANT role2 TO mike")

        expected_permissions = [("mike", "<table ks.cf>", "MODIFY"),
                                ("role1", "<table ks.cf>", "SELECT"),
                                ("role2", "<table ks.cf>", "ALTER"),
                                ("role2", "<role role1>", "ALTER")]
        expected_permissions.extend(data_resource_creator_permissions('cassandra', '<keyspace ks>'))
        expected_permissions.extend(data_resource_creator_permissions('cassandra', '<table ks.cf>'))
        expected_permissions.extend(role_creator_permissions('cassandra', '<role mike>'))
        expected_permissions.extend(role_creator_permissions('cassandra', '<role role1>'))
        expected_permissions.extend(role_creator_permissions('cassandra', '<role role2>'))

        self.assert_permissions_listed(expected_permissions, cassandra, "LIST ALL PERMISSIONS")

        self.assert_permissions_listed([("role1", "<table ks.cf>", "SELECT")],
                                       cassandra,
                                       "LIST ALL PERMISSIONS OF role1")

        self.assert_permissions_listed([("role1", "<table ks.cf>", "SELECT"),
                                        ("role2", "<table ks.cf>", "ALTER"),
                                        ("role2", "<role role1>", "ALTER")],
                                       cassandra,
                                       "LIST ALL PERMISSIONS OF role2")

        self.assert_permissions_listed([("cassandra", "<role role1>", "ALTER"),
                                        ("cassandra", "<role role1>", "DROP"),
                                        ("cassandra", "<role role1>", "AUTHORIZE"),
                                        ("role2", "<role role1>", "ALTER")],
                                       cassandra,
                                       "LIST ALL PERMISSIONS ON ROLE role1")
        # we didn't specifically grant DROP on role1, so only it's creator should have it
        self.assert_permissions_listed([("cassandra", "<role role1>", "DROP")],
                                       cassandra,
                                       "LIST DROP PERMISSION ON ROLE role1")
        # but we did specifically grant ALTER role1 to role2
        # so that should be listed whether we include an OF clause or not
        self.assert_permissions_listed([("cassandra", "<role role1>", "ALTER"),
                                        ("role2", "<role role1>", "ALTER")],
                                       cassandra,
                                       "LIST ALTER PERMISSION ON ROLE role1")
        self.assert_permissions_listed([("role2", "<role role1>", "ALTER")],
                                       cassandra,
                                       "LIST ALTER PERMISSION ON ROLE role1 OF role2")
        # make sure ALTER on role2 is excluded properly when OF is for another role
        cassandra.execute("CREATE ROLE role3 WITH SUPERUSER = false AND LOGIN = false")
        assert cassandra.execute("LIST ALTER PERMISSION ON ROLE role1 OF role3") is None

        # now check users can list their own permissions
        mike = self.get_session(user='mike', password='12345')
        self.assert_permissions_listed([("mike", "<table ks.cf>", "MODIFY"),
                                        ("role1", "<table ks.cf>", "SELECT"),
                                        ("role2", "<table ks.cf>", "ALTER"),
                                        ("role2", "<role role1>", "ALTER")],
                                       mike,
                                       "LIST ALL PERMISSIONS OF mike")

    def list_permissions_validation_test(self):
        self.prepare()

        cassandra = self.get_session(user='cassandra', password='cassandra')
        cassandra.execute("CREATE KEYSPACE ks WITH replication = {'class':'SimpleStrategy', 'replication_factor':1}")
        cassandra.execute("CREATE TABLE ks.cf (id int primary key, val int)")
        cassandra.execute("CREATE ROLE mike WITH PASSWORD = '12345' AND SUPERUSER = false AND LOGIN = true")
        cassandra.execute("CREATE ROLE john WITH PASSWORD = '12345' AND SUPERUSER = false AND LOGIN = true")
        cassandra.execute("CREATE ROLE role1")
        cassandra.execute("CREATE ROLE role2")

        cassandra.execute("GRANT SELECT ON table ks.cf TO role1")
        cassandra.execute("GRANT ALTER ON table ks.cf TO role2")
        cassandra.execute("GRANT MODIFY ON table ks.cf TO john")

        cassandra.execute("GRANT role1 TO role2")
        cassandra.execute("GRANT role2 TO mike")

        mike = self.get_session(user='mike', password='12345')

        self.assert_permissions_listed([("role1", "<table ks.cf>", "SELECT"),
                                        ("role2", "<table ks.cf>", "ALTER")],
                                       mike,
                                       "LIST ALL PERMISSIONS OF role2")

        self.assert_permissions_listed([("role1", "<table ks.cf>", "SELECT")],
                                       mike,
                                       "LIST ALL PERMISSIONS OF role1")

        assert_invalid(mike,
                       "LIST ALL PERMISSIONS",
                       "You are not authorized to view everyone's permissions",
                       Unauthorized)
        assert_invalid(mike,
                       "LIST ALL PERMISSIONS OF john",
                       "You are not authorized to view john's permissions",
                       Unauthorized)

    def role_caching_authenticated_user_test(self):
        # This test is to show that the role caching in AuthenticatedUser
        # works correctly and revokes the roles from a logged in user
        self.prepare(roles_expiry=2000)
        cassandra = self.get_session(user='cassandra', password='cassandra')
        cassandra.execute("CREATE KEYSPACE ks WITH replication = {'class':'SimpleStrategy', 'replication_factor':1}")
        cassandra.execute("CREATE TABLE ks.cf (id int primary key, val int)")
        cassandra.execute("CREATE ROLE mike WITH PASSWORD = '12345' AND SUPERUSER = false AND LOGIN = true")
        cassandra.execute("CREATE ROLE role1")
        cassandra.execute("GRANT ALL ON table ks.cf TO role1")
        cassandra.execute("GRANT role1 TO mike")

        mike = self.get_session(user='mike', password='12345')
        mike.execute("INSERT INTO ks.cf (id, val) VALUES (0, 0)")

        assert_one(mike, "SELECT * FROM ks.cf", [0, 0])

        cassandra.execute("REVOKE role1 FROM mike")
        # mike should retain permissions until the cache expires
        unauthorized = None
        cnt = 0
        while not unauthorized and cnt < 20:
            try:
                mike.execute("SELECT * FROM ks.cf")
                cnt += 1
                time.sleep(.5)
            except Unauthorized as e:
                unauthorized = e

        assert unauthorized is not None

    def prevent_circular_grants_test(self):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        cassandra.execute("CREATE ROLE mike")
        cassandra.execute("CREATE ROLE role1")
        cassandra.execute("CREATE ROLE role2")
        cassandra.execute("GRANT role2 to role1")
        cassandra.execute("GRANT role1 TO mike")
        assert_invalid(cassandra,
                       "GRANT mike TO role1",
                       "mike is a member of role1",
                       InvalidRequest)
        assert_invalid(cassandra,
                       "GRANT mike TO role2",
                       "mike is a member of role2",
                       InvalidRequest)

    def create_user_as_alias_for_create_role_test(self):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        cassandra.execute("CREATE USER mike WITH PASSWORD '12345' NOSUPERUSER")
        assert_one(cassandra, "LIST ROLES OF mike", mike_role)

        cassandra.execute("CREATE USER super_user WITH PASSWORD '12345' SUPERUSER")
        assert_one(cassandra, "LIST ROLES OF super_user", ["super_user", True, True, {}])

    def role_requires_login_privilege_to_authenticate_test(self):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        cassandra.execute("CREATE ROLE mike WITH PASSWORD = '12345' AND SUPERUSER = false AND LOGIN = true")
        assert_one(cassandra, "LIST ROLES OF mike", mike_role)
        self.get_session(user='mike', password='12345')

        cassandra.execute("ALTER ROLE mike WITH LOGIN = false")
        assert_one(cassandra, "LIST ROLES OF mike", ["mike", False, False, {}])
        self.assert_unauthenticated('mike is not permitted to log in', 'mike', '12345')

        cassandra.execute("ALTER ROLE mike WITH LOGIN = true")
        assert_one(cassandra, "LIST ROLES OF mike", ["mike", False, True, {}])
        self.get_session(user='mike', password='12345')

    def roles_do_not_inherit_login_privilege_test(self):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        cassandra.execute("CREATE ROLE mike WITH PASSWORD = '12345' AND SUPERUSER = false AND LOGIN = false")
        cassandra.execute("CREATE ROLE with_login WITH PASSWORD = '54321' AND SUPERUSER = false AND LOGIN = true")
        cassandra.execute("GRANT with_login to mike")

        assert_all(cassandra, "LIST ROLES OF mike", [["mike", False, False, {}],
                                                     ["with_login", False, True, {}]])
        assert_one(cassandra, "LIST ROLES OF with_login", ["with_login", False, True, {}])

        self.assert_unauthenticated("mike is not permitted to log in", "mike", "12345")

    def role_requires_password_to_login_test(self):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        cassandra.execute("CREATE ROLE mike WITH SUPERUSER = false AND LOGIN = true")
        self.assert_unauthenticated("Username and/or password are incorrect", 'mike', None)
        cassandra.execute("ALTER ROLE mike WITH PASSWORD = '12345'")
        self.get_session(user='mike', password='12345')

    def superuser_status_is_inherited_test(self):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        cassandra.execute("CREATE ROLE mike WITH PASSWORD = '12345' AND SUPERUSER = false AND LOGIN = true")
        cassandra.execute("CREATE ROLE db_admin WITH SUPERUSER = true")

        mike = self.get_session(user='mike', password='12345')
        assert_invalid(mike,
                       "CREATE ROLE another_role WITH SUPERUSER = false AND LOGIN = false",
                       "User mike does not have sufficient privileges to perform the requested operation",
                       Unauthorized)

        cassandra.execute("GRANT db_admin TO mike")
        mike.execute("CREATE ROLE another_role WITH SUPERUSER = false AND LOGIN = false")
        assert_all(mike, "LIST ROLES", [["another_role", False, False, {}],
                                        cassandra_role,
                                        ["db_admin", True, False, {}],
                                        mike_role])

    def list_users_considers_inherited_superuser_status_test(self):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        cassandra.execute("CREATE ROLE db_admin WITH SUPERUSER = true")
        cassandra.execute("CREATE ROLE mike WITH PASSWORD = '12345' AND SUPERUSER = false AND LOGIN = true")
        cassandra.execute("GRANT db_admin TO mike")
        assert_all(cassandra, "LIST USERS", [['cassandra', True],
                                             ["mike", True]])

    # UDF permissions tests TODO move to separate fixture & refactor this + auth_test.py
    def grant_revoke_udf_permissions_test(self):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        self.setup_table(cassandra)
        cassandra.execute("CREATE ROLE mike WITH PASSWORD = '12345' AND LOGIN = true")
        cassandra.execute("CREATE FUNCTION ks.plus_one ( input int ) RETURNS int LANGUAGE javascript AS 'input + 1'")
        cassandra.execute("CREATE FUNCTION ks.\"plusOne\" ( input int ) RETURNS int LANGUAGE javascript AS 'input + 1'")

        # grant / revoke on a specific function
        cassandra.execute("GRANT EXECUTE ON FUNCTION ks.plus_one(int) TO mike")
        cassandra.execute("GRANT EXECUTE ON FUNCTION ks.\"plusOne\"(int) TO mike")

        self.assert_permissions_listed([("mike", "<function ks.plus_one(int)>", "EXECUTE"),
                                        ("mike", "<function ks.plusOne(int)>", "EXECUTE")],
                                       cassandra,
                                       "LIST ALL PERMISSIONS OF mike")
        cassandra.execute("REVOKE ALL PERMISSIONS ON FUNCTION ks.plus_one(int) FROM mike")
        self.assert_permissions_listed([("mike", "<function ks.plusOne(int)>", "EXECUTE")],
                                       cassandra,
                                       "LIST ALL PERMISSIONS OF mike")
        cassandra.execute("REVOKE EXECUTE PERMISSION ON FUNCTION ks.\"plusOne\"(int) FROM mike")
        self.assert_no_permissions(cassandra, "LIST ALL PERMISSIONS OF mike")

        # grant / revoke on all functions in a given keyspace
        cassandra.execute("GRANT EXECUTE PERMISSION ON ALL FUNCTIONS IN KEYSPACE ks TO mike")
        self.assert_permissions_listed([("mike", "<all functions in ks>", "EXECUTE")],
                                       cassandra,
                                       "LIST ALL PERMISSIONS OF mike")
        cassandra.execute("REVOKE EXECUTE PERMISSION ON ALL FUNCTIONS IN KEYSPACE ks FROM mike")
        self.assert_no_permissions(cassandra, "LIST ALL PERMISSIONS OF mike")

        # grant / revoke on all (non-builtin) functions
        cassandra.execute("GRANT EXECUTE PERMISSION ON ALL FUNCTIONS TO mike")
        self.assert_permissions_listed([("mike", "<all functions>", "EXECUTE")],
                                       cassandra,
                                       "LIST ALL PERMISSIONS OF mike")
        cassandra.execute("REVOKE EXECUTE PERMISSION ON ALL FUNCTIONS FROM mike")
        self.assert_no_permissions(cassandra, "LIST ALL PERMISSIONS OF mike")

    def udf_permissions_validation_test(self):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        self.setup_table(cassandra)
        cassandra.execute("CREATE FUNCTION ks.plus_one ( input int ) RETURNS int LANGUAGE javascript AS 'input + 1'")
        cassandra.execute("CREATE ROLE mike WITH PASSWORD = '12345' AND LOGIN = true")
        mike = self.get_session(user='mike', password='12345')

        # can't replace an existing function without ALTER permission on the parent ks
        cql = "CREATE OR REPLACE FUNCTION ks.plus_one( input int ) RETURNS int LANGUAGE javascript as '1 + input'"
        assert_invalid(mike, cql,
                       "User mike has no ALTER permission on <function ks.plus_one\(int\)> or any of its parents",
                       Unauthorized)
        cassandra.execute("GRANT ALTER ON FUNCTION ks.plus_one(int) TO mike")
        mike.execute(cql)

        # can't grant permissions on a function without AUTHORIZE (and without the grantor having EXECUTE themself)
        cassandra.execute("GRANT EXECUTE ON FUNCTION ks.plus_one(int) TO mike")
        cassandra.execute("CREATE ROLE role1")
        cql = "GRANT EXECUTE ON FUNCTION ks.plus_one(int) TO role1"
        assert_invalid(mike, cql,
                       "User mike has no AUTHORIZE permission on <function ks.plus_one\(int\)> or any of its parents",
                       Unauthorized)
        cassandra.execute("GRANT AUTHORIZE ON FUNCTION ks.plus_one(int) TO mike")
        mike.execute(cql)
        # now revoke AUTHORIZE from mike
        cassandra.execute("REVOKE AUTHORIZE ON FUNCTION ks.plus_one(int) FROM mike")
        cql = "REVOKE EXECUTE ON FUNCTION ks.plus_one(int) FROM role1"
        assert_invalid(mike, cql,
                       "User mike has no AUTHORIZE permission on <function ks.plus_one\(int\)> or any of its parents",
                       Unauthorized)
        cassandra.execute("GRANT AUTHORIZE ON FUNCTION ks.plus_one(int) TO mike")
        mike.execute(cql)

        # can't drop a function without DROP on the parent keyspace
        cql = "DROP FUNCTION ks.plus_one(int)"
        assert_invalid(mike, cql,
                       "User mike has no DROP permission on <function ks.plus_one\(int\)> or any of its parents",
                       Unauthorized)
        cassandra.execute("GRANT DROP ON FUNCTION ks.plus_one(int) TO mike")
        mike.execute(cql)

        # can't create a new function without CREATE on the parent keyspace's collection of functions
        cql = "CREATE FUNCTION ks.plus_one ( input int ) RETURNS int LANGUAGE javascript AS 'input + 1'"
        assert_invalid(mike, cql,
                       "User mike has no CREATE permission on <all functions in ks> or any of its parents",
                       Unauthorized)
        cassandra.execute("GRANT CREATE ON ALL FUNCTIONS IN KEYSPACE ks TO mike")
        mike.execute(cql)

## todo some tests for overloads (DROP, CREATE etc)

    def drop_role_cleans_up_udf_permissions_test(self):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        self.setup_table(cassandra)
        cassandra.execute("CREATE ROLE mike WITH PASSWORD = '12345' AND LOGIN = true")
        cassandra.execute("CREATE FUNCTION ks.plus_one ( input int ) RETURNS int LANGUAGE javascript AS 'input + 1'")
        cassandra.execute("GRANT EXECUTE ON FUNCTION ks.plus_one(int) TO mike")
        cassandra.execute("GRANT EXECUTE ON ALL FUNCTIONS IN KEYSPACE ks TO mike")
        cassandra.execute("GRANT EXECUTE ON ALL FUNCTIONS TO mike")

        self.assert_permissions_listed([("mike", "<all functions>", "EXECUTE"),
                                        ("mike", "<all functions in ks>", "EXECUTE"),
                                        ("mike", "<function ks.plus_one(int)>", "EXECUTE")],
                                       cassandra,
                                       "LIST ALL PERMISSIONS OF mike")

        # drop and recreate the role to ensure permissions are cleared
        cassandra.execute("DROP ROLE mike")
        cassandra.execute("CREATE ROLE mike WITH PASSWORD = '12345' AND LOGIN = true")
        self.assert_no_permissions(cassandra, "LIST ALL PERMISSIONS OF mike")

    def drop_function_and_keyspace_cleans_up_udf_permissions_test(self):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        self.setup_table(cassandra)
        cassandra.execute("CREATE ROLE mike WITH PASSWORD = '12345' AND LOGIN = true")
        cassandra.execute("CREATE FUNCTION ks.plus_one ( input int ) RETURNS int LANGUAGE javascript AS 'input + 1'")
        cassandra.execute("GRANT EXECUTE ON FUNCTION ks.plus_one(int) TO mike")
        cassandra.execute("GRANT EXECUTE ON ALL FUNCTIONS IN KEYSPACE ks TO mike")

        self.assert_permissions_listed([("mike", "<all functions in ks>", "EXECUTE"),
                                        ("mike", "<function ks.plus_one(int)>", "EXECUTE")],
                                       cassandra,
                                       "LIST ALL PERMISSIONS OF mike")

        # drop the function
        cassandra.execute("DROP FUNCTION ks.plus_one")
        self.assert_permissions_listed([("mike", "<all functions in ks>", "EXECUTE")],
                                       cassandra,
                                       "LIST ALL PERMISSIONS OF mike")
        # drop the keyspace
        cassandra.execute("DROP KEYSPACE ks")
        self.assert_no_permissions(cassandra, "LIST ALL PERMISSIONS OF mike")

    def udf_permissions_in_selection_test(self):
        self.verify_udf_permissions("SELECT k, v, ks.plus_one(v) FROM ks.t1 WHERE k = 1")

    def udf_permissions_in_select_where_clause_test(self):
        self.verify_udf_permissions("SELECT k, v FROM ks.t1 WHERE k = ks.plus_one(0)")

    def udf_permissions_in_insert_test(self):
        self.verify_udf_permissions("INSERT INTO ks.t1 (k, v) VALUES (1, ks.plus_one(1))")

    def udf_permissions_in_update_test(self):
        self.verify_udf_permissions("UPDATE ks.t1 SET v = ks.plus_one(2) WHERE k = ks.plus_one(0)")

    def udf_permissions_in_delete_test(self):
        self.verify_udf_permissions("DELETE FROM ks.t1 WHERE k = ks.plus_one(0)")

    def verify_udf_permissions(self, cql):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        self.setup_table(cassandra)
        cassandra.execute("CREATE FUNCTION ks.plus_one ( input int ) RETURNS int LANGUAGE javascript AS 'input + 1'")
        cassandra.execute("CREATE ROLE mike WITH PASSWORD = '12345' AND LOGIN = true")
        cassandra.execute("GRANT ALL PERMISSIONS ON ks.t1 TO mike")
        cassandra.execute("INSERT INTO ks.t1 (k,v) values (1,1)")
        mike = self.get_session(user='mike', password='12345')
        assert_invalid(mike,
                       cql,
                       "User mike has no EXECUTE permission on <function ks.plus_one\(int\)> or any of its parents",
                       Unauthorized)

        cassandra.execute("GRANT EXECUTE ON FUNCTION ks.plus_one(int) TO mike")
        return mike.execute(cql)

    def inheritence_of_udf_permissions_test(self):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        self.setup_table(cassandra)
        cassandra.execute("CREATE ROLE function_user")
        cassandra.execute("GRANT EXECUTE ON ALL FUNCTIONS IN KEYSPACE ks TO function_user")
        cassandra.execute("CREATE FUNCTION ks.plus_one ( input int ) RETURNS int LANGUAGE javascript AS 'input + 1'")
        cassandra.execute("INSERT INTO ks.t1 (k,v) VALUES (1,1)")
        cassandra.execute("CREATE ROLE mike WITH PASSWORD = '12345' AND LOGIN = true")
        cassandra.execute("GRANT SELECT ON ks.t1 TO mike")
        mike = self.get_session(user='mike', password='12345')
        select = "SELECT k, v, ks.plus_one(v) FROM ks.t1 WHERE k = 1"
        assert_invalid(mike,
                       select,
                       "User mike has no EXECUTE permission on <function ks.plus_one\(int\)> or any of its parents",
                       Unauthorized)

        cassandra.execute("GRANT EXECUTE ON FUNCTION ks.plus_one(int) TO mike")
        assert_one(mike, select, [1, 1, 2])

    def builtin_functions_require_no_special_permissions_test(self):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        cassandra.execute("CREATE ROLE mike WITH PASSWORD = '12345' AND LOGIN = true")
        self.setup_table(cassandra)
        cassandra.execute("INSERT INTO ks.t1 (k,v) VALUES (1,1)")
        mike = self.get_session(user='mike', password='12345')
        cassandra.execute("GRANT ALL PERMISSIONS ON ks.t1 TO mike")
        assert_one(mike, "SELECT * from ks.t1 WHERE k=blobasint(intasblob(1))", [1, 1])

    def aggregate_function_permissions_test(self):
        self.prepare()
        cassandra = self.get_session(user='cassandra', password='cassandra')
        self.setup_table(cassandra)
        cassandra.execute("INSERT INTO ks.t1 (k,v) VALUES (1,1)")
        cassandra.execute("INSERT INTO ks.t1 (k,v) VALUES (2,2)")
        cassandra.execute("""CREATE FUNCTION ks.state_function( a int, b int )
                             RETURNS int
                             LANGUAGE java
                             AS 'return Integer.valueOf( (a != null ? a.intValue() : 0) + b.intValue());'""")
        cassandra.execute("""CREATE FUNCTION ks.final_function( a int )
                             RETURNS int
                             LANGUAGE java
                             AS 'return a;'""")
        cassandra.execute("CREATE ROLE mike WITH PASSWORD = '12345' AND LOGIN = true")
        cassandra.execute("GRANT CREATE ON ALL FUNCTIONS IN KEYSPACE ks TO mike")
        cassandra.execute("GRANT ALL PERMISSIONS ON ks.t1 TO mike")
        mike = self.get_session(user='mike', password='12345')
        create_aggregate_cql = """CREATE AGGREGATE ks.simple_aggregate(int)
                          SFUNC state_function
                          STYPE int
                          FINALFUNC final_function
                          INITCOND 0"""
        # check permissions to create the aggregate
        assert_invalid(mike,
                       create_aggregate_cql,
                       "User mike has no EXECUTE permission on <function ks.state_function\(int, int\)> or any of its parents",
                       Unauthorized)
        cassandra.execute("GRANT EXECUTE ON FUNCTION ks.state_function(int, int) TO mike")
        assert_invalid(mike,
                       create_aggregate_cql,
                       "User mike has no EXECUTE permission on <function ks.final_function\(int\)> or any of its parents",
                       Unauthorized)
        cassandra.execute("GRANT EXECUTE ON FUNCTION ks.final_function(int) TO mike")
        mike.execute(create_aggregate_cql)

        # without execute permissions on the state or final function we
        # cannot use the aggregate, so revoke them to verify
        cassandra.execute("REVOKE EXECUTE ON FUNCTION ks.state_function(int, int) FROM mike")
        cassandra.execute("REVOKE EXECUTE ON FUNCTION ks.final_function(int) FROM mike")
        execute_aggregate_cql = "SELECT ks.simple_aggregate(v) FROM ks.t1"
        assert_invalid(mike,
                       execute_aggregate_cql,
                       "User mike has no EXECUTE permission on <function ks.state_function\(int, int\)> or any of its parents",
                       Unauthorized)
        cassandra.execute("GRANT EXECUTE ON FUNCTION ks.state_function(int, int) TO mike")
        assert_invalid(mike,
                       execute_aggregate_cql,
                       "User mike has no EXECUTE permission on <function ks.final_function\(int\)> or any of its parents",
                       Unauthorized)
        cassandra.execute("GRANT EXECUTE ON FUNCTION ks.final_function(int) TO mike")

        # mike *does* have execute permission on the aggregate function, as its creator
        assert_one(mike, execute_aggregate_cql, [3])

    def setup_table(self, session):
        session.execute("CREATE KEYSPACE ks WITH REPLICATION = {'class':'SimpleStrategy', 'replication_factor':1}")
        session.execute("CREATE TABLE ks.t1 (k int PRIMARY KEY, v int)")

    def assert_unauthenticated(self, message, user, password):
        with self.assertRaises(NoHostAvailable) as response:
            node = self.cluster.nodelist()[0]
            self.cql_connection(node, version="3.1.7", user=user, password=password)
        host, error = response.exception.errors.popitem()
        pattern = 'Failed to authenticate to %s: code=0100 \[Bad credentials\] message="%s"' % (host, message)
        assert type(error) == AuthenticationFailed, "Expected AuthenticationFailed, got %s" % type(error)
        assert re.search(pattern, error.message), "Expected: %s" % pattern

    def prepare(self, nodes=1, roles_expiry=0):
        config = {'authenticator': 'org.apache.cassandra.auth.PasswordAuthenticator',
                  'authorizer': 'org.apache.cassandra.auth.CassandraAuthorizer',
                  'role_manager': 'org.apache.cassandra.auth.CassandraRoleManager',
                  'permissions_validity_in_ms': 0,
                  'roles_validity_in_ms': roles_expiry}
        self.cluster.set_configuration_options(values=config)
        self.cluster.populate(nodes).start(no_wait=True)
        # default user setup is delayed by 10 seconds to reduce log spam

        if nodes == 1:
            self.cluster.nodelist()[0].watch_log_for("Created default superuser")
        else:
            # can' just watch for log - the line will appear in just one of the nodes' logs
            # only one test uses more than 1 node, though, so some sleep is fine.
            time.sleep(15)

    def get_session(self, node_idx=0, user=None, password=None):
        node = self.cluster.nodelist()[node_idx]
        conn = self.patient_cql_connection(node, version="3.1.7", user=user, password=password)
        return conn

    def assert_permissions_listed(self, expected, cursor, query):
        rows = cursor.execute(query)
        perms = [(str(r.role), str(r.resource), str(r.permission)) for r in rows]
        self.assertEqual(sorted(expected), sorted(perms))

    def assert_no_permissions(self, cursor, query):
        assert cursor.execute(query) is None
